"""Durable tracking and conservative retention for Discord webhook messages."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def console_handlers(formatter):
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(logging.DEBUG)
    stdout.addFilter(lambda record: record.levelno < logging.WARNING)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING)
    for handler in (stdout, stderr):
        handler.setFormatter(formatter)
    return stdout, stderr


def post_url(webhook_url):
    parts = urlsplit(webhook_url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "wait"]
    query.append(("wait", "true"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def message_url(webhook_url, message_id):
    parts = urlsplit(webhook_url)
    query = [
        (key, value) for key, value in parse_qsl(parts.query) if key == "thread_id"
    ]
    return urlunsplit(
        parts._replace(
            path=parts.path.rstrip("/") + "/messages/" + str(message_id),
            query=urlencode(query),
        )
    )


def webhook_id(webhook_url):
    parts = urlsplit(webhook_url).path.split("/")
    try:
        return parts[parts.index("webhooks") + 1]
    except (ValueError, IndexError):
        return None


def error_code(body):
    return body.get("code") if isinstance(body, dict) else None


def retry_delay(body):
    try:
        delay = float(body.get("retry_after", 1)) if isinstance(body, dict) else 1.0
        return max(delay, 0) if math.isfinite(delay) else 1.0
    except (TypeError, ValueError):
        return 1.0


def deletion_confirmed(status, body):
    # Unknown webhook/token/channel is not evidence that a message was deleted.
    return status == 204 or (status == 404 and error_code(body) == 10008)


def legacy_message_matches(body, message_id, webhook_url):
    return (
        isinstance(body, dict)
        and str(body.get("id")) == str(message_id)
        and webhook_id(webhook_url) is not None
        and str(body.get("webhook_id")) == webhook_id(webhook_url)
    )


def parse_time(value):
    parsed = datetime.fromisoformat(value)
    # Old records used local naive timestamps. Preserve their local meaning.
    return parsed.astimezone(timezone.utc)


def cleanup_due(last_cleanup, interval_hours):
    if not last_cleanup:
        return True
    try:
        return datetime.now(timezone.utc) - parse_time(last_cleanup) >= timedelta(
            hours=interval_hours
        )
    except (TypeError, ValueError):
        return True


def max_age_hours(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("DISCORD_MESSAGE_MAX_AGE_HOURS must be finite and nonnegative")
    return result


def stale_rows(rows, keep, max_age_hours=0):
    """Rows are (message_id, posted_at, webhook_url), newest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    result = []

    def chronology(row):
        try:
            return parse_time(row[1])
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    for index, row in enumerate(sorted(rows, key=chronology, reverse=True)):
        expired = False
        if max_age_hours > 0:
            try:
                expired = parse_time(row[1]) <= cutoff
            except (TypeError, ValueError):
                pass
        if index >= max(keep, 0) or expired:
            result.append(row)
    return result


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS discord_messages (
        message_id TEXT PRIMARY KEY, posted_at TEXT NOT NULL, webhook_url TEXT
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(discord_messages)")}
    if "webhook_url" not in columns:
        # Legacy provenance is unknown. Verify it with GET before assigning a route.
        conn.execute("ALTER TABLE discord_messages ADD COLUMN webhook_url TEXT")


def _insert(db_path, entry):
    with closing(sqlite3.connect(str(db_path), timeout=10)) as conn, conn:
        conn.execute(
            """INSERT OR IGNORE INTO discord_messages
            (message_id, posted_at, webhook_url) VALUES (?, ?, ?)""",
            (entry["message_id"], entry["posted_at"], entry["webhook_url"]),
        )


def _pending_dir(db_path):
    path = Path(db_path)
    return path.with_name(path.name + ".discord-pending")


def record_message(db_path, message_id, webhook_url, logger):
    """Persist a received ID before relying on the SQLite write succeeding.

    A private journal survives a failed/locked DB and is replayed by cleanup.
    This cannot recover the ID of a POST whose response never arrived.
    """
    if not message_id or not str(message_id).isdigit() or not webhook_url:
        raise ValueError("A Discord message ID and its webhook route are required")
    entry = {
        "message_id": str(message_id),
        "webhook_url": webhook_url,
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }
    journal = None
    temporary = None
    try:
        directory = _pending_dir(db_path)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(entry, stream)
            stream.flush()
            os.fsync(stream.fileno())
        destination = directory / (str(message_id) + ".json")
        os.replace(temporary, destination)
        temporary = None
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        journal = destination
    except OSError:
        # Try SQLite even if the journal filesystem is unavailable.
        logger.error("could not journal Discord message id=%s", message_id)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    try:
        _insert(db_path, entry)
    except sqlite3.Error as error:
        if journal is not None:
            logger.error(
                "Discord message queued for DB recovery id=%s error_type=%s",
                message_id,
                type(error).__name__,
            )
            return
        raise RuntimeError(
            "Discord message ID could not be persisted: " + str(message_id)
        ) from error
    if journal is not None:
        try:
            journal.unlink()
        except OSError:
            logger.warning("Discord journal removal deferred id=%s", message_id)


def recover_pending(db_path, logger):
    complete = True
    for path in _pending_dir(db_path).glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(entry, dict)
                or not str(entry.get("message_id", "")).isdigit()
                or not entry.get("webhook_url")
                or not entry.get("posted_at")
            ):
                raise ValueError("invalid pending message")
            _insert(db_path, entry)
            path.unlink()
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
            complete = False
            logger.error(
                "Discord message recovery deferred file=%s error_type=%s",
                path.name,
                type(error).__name__,
            )
    return complete
