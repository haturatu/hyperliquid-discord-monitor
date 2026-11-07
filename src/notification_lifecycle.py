"""Durable Discord notification sessions: POST -> PATCH updates -> final POST.

Each wallet/coin/destination owns a session. Fills are never discarded merely
because another notification was sent recently. SQLite persists the latest fill
and message ID, so the idle sweep survives process restarts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

import requests

from . import discord_retention as retention

logger = logging.getLogger("hdm.notifications")


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_sessions (
            coin TEXT NOT NULL,
            webhook_url TEXT NOT NULL,
            first_embed TEXT NOT NULL,
            latest_fill TEXT NOT NULL,
            tag TEXT,
            event_count INTEGER NOT NULL,
            updated_count INTEGER NOT NULL DEFAULT 0,
            message_id TEXT,
            channel_id TEXT,
            weighted_price_sum REAL,
            filled_size REAL,
            last_event_at REAL NOT NULL,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (coin, webhook_url)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            webhook_url TEXT NOT NULL,
            message_id TEXT NOT NULL,
            completed_embed TEXT NOT NULL,
            next_attempt_at REAL NOT NULL DEFAULT 0
        )
    """)
    # Existing live sessions cannot reconstruct a historical weighted average
    # from their stored last fill. Leave it unknown until the next session.
    columns = {column[1] for column in conn.execute("PRAGMA table_info(notification_sessions)")}
    for name, sql_type in (
        ("weighted_price_sum", "REAL"),
        ("filled_size", "REAL"),
        ("channel_id", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE notification_sessions ADD COLUMN {name} {sql_type}")


def _fill_snapshot(trade, ghost_url=None):
    # Ordinary perp markets use USDC. HIP-3 collateral is resolved by the
    # caller using Hyperliquid metadata and must not be guessed here.
    if ghost_url is None and ":" not in trade.coin:
        ghost_url = (
            f"https://app.hyperliquid.xyz/trade/{trade.coin}/USDC"
            f"?hloa={trade.address}"
        )
    return {
        "address": trade.address, "coin": trade.coin,
        "direction": trade.direction, "price": trade.price,
        "size": trade.size, "tx_hash": trade.tx_hash,
        "timestamp": trade.timestamp.isoformat() if trade.timestamp else "",
        "ghost_url": ghost_url,
    }


def enqueue(
    db_path, trade, tag, webhook_urls, first_embed=None, now=None,
    trade_uid=None, ghost_url=None,
):
    """Persist a new fill for each target. Caller already deduplicated the fill."""
    now = time.time() if now is None else now
    snapshot_data = _fill_snapshot(trade, ghost_url=ghost_url)
    snapshot = json.dumps(snapshot_data, ensure_ascii=False)
    filled_size = abs(float(trade.size))
    weighted_price_sum = filled_size * float(trade.price)
    if first_embed is None:
        first_embed = _initial_embed(trade, tag, snapshot_data["ghost_url"])
    with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        ensure_schema(conn)
        if trade_uid is not None:
            inserted = conn.execute("""
                INSERT OR IGNORE INTO trades
                    (trade_uid, tx_hash, address, coin, direction, timestamp, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_uid, trade.tx_hash, trade.address, trade.coin,
                trade.direction,
                trade.timestamp.isoformat() if trade.timestamp else None,
                int(now),
            ))
            if inserted.rowcount == 0:
                return False
        for webhook_url in dict.fromkeys(url for url in webhook_urls if url):
            conn.execute("""
                INSERT INTO notification_sessions
                    (coin, webhook_url, first_embed, latest_fill, tag,
                     event_count, last_event_at, weighted_price_sum, filled_size)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(coin, webhook_url) DO UPDATE SET
                    latest_fill=excluded.latest_fill,
                    tag=excluded.tag,
                    event_count=notification_sessions.event_count + 1,
                    last_event_at=excluded.last_event_at,
                    weighted_price_sum=CASE
                        WHEN notification_sessions.weighted_price_sum IS NULL THEN NULL
                        ELSE notification_sessions.weighted_price_sum + excluded.weighted_price_sum
                    END,
                    filled_size=CASE
                        WHEN notification_sessions.filled_size IS NULL THEN NULL
                        ELSE notification_sessions.filled_size + excluded.filled_size
                    END
            """, (
                trade.coin, webhook_url,
                json.dumps(first_embed, ensure_ascii=False), snapshot,
                tag, now, weighted_price_sum, filled_size,
            ))
    return True


def _direction_color(direction):
    """Keep direction color consistent across initial, live and finished cards."""
    return 0x00FF00 if "Long" in direction else 0x0099FF


def _initial_embed(trade, tag, ghost_url=None):
    direction = trade.direction or "Unknown"
    if ghost_url is None:
        ghost_url = _fill_snapshot(trade)["ghost_url"]
    marker = "📈" if "Long" in direction else "📉"
    title = f"{marker} {trade.coin} {direction}"
    if tag:
        title += f" (Tag: {tag})"
    return {
        "title": title,
        "description": (
            f"最初のエントリーを検知\n"
            f"Coin: {trade.coin}\n"
            f"Price: {trade.price}\n"
            f"Direction: {direction}\n"
            f"Address: https://hypurrscan.io/address/{trade.address}"
            + (f"\nGhost: {ghost_url}" if ghost_url else "")
        ),
        "color": _direction_color(direction),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Tx: {trade.tx_hash}"},
    }


def active_message_ids(conn):
    """Do not delete active leading POSTs during Discord retention sweeps."""
    ensure_schema(conn)
    return {
        row[0] for row in conn.execute("""
            SELECT message_id FROM notification_sessions
            WHERE message_id IS NOT NULL AND message_id != '__unknown__'
            UNION
            SELECT message_id FROM notification_completions
        """)
    }


def _origin_message_link(row, channel_id=None):
    """Use the actual Discord channel ID, never the webhook ID."""
    channel_id = channel_id or row["channel_id"]
    message_id = row["message_id"]
    if not channel_id or not message_id:
        return None
    if not str(channel_id).isdigit() or not str(message_id).isdigit():
        return None
    return f"https://discord.com/channels/@me/{channel_id}/{message_id}"


def _session_embed(row, final=False, completed=False, channel_id=None):
    fill = json.loads(row["latest_fill"])
    direction = fill.get("direction") or "Unknown"
    coin = fill.get("coin") or row["coin"]
    marker = "✅" if final or completed else "📈" if "Long" in direction else "📉"
    suffix = "ポジション変更完了" if final else "更新完了" if completed else "更新中"
    title = f"{marker} {coin} {suffix}"
    if row["tag"]:
        title += f" (Tag: {row['tag']})"
    if final:
        # Keep the separate final POST format unchanged.
        price_line = f"最終約定価格: {fill.get('price')}"
    else:
        size = row["filled_size"]
        weighted_sum = row["weighted_price_sum"]
        if size is None or weighted_sum is None:
            average = "集計不可（更新前の約定履歴が不足）"
        elif size <= 0:
            average = "集計不可（約定サイズ0）"
        else:
            average = format(weighted_sum / size, ".12g")
        price_line = f"平均約定価格: {average}"
    origin_link = _origin_message_link(row, channel_id) if final else None
    description = (
        f"約定数: {row['event_count']}回\n"
        f"最新方向: {direction}\n"
        f"{price_line}\n"
        f"最終約定サイズ: {fill.get('size')}\n"
        f"Address: https://hypurrscan.io/address/{fill.get('address')}"
        + (f"\nGhost: {fill['ghost_url']}" if fill.get("ghost_url") else "")
        + (f"\n投稿元: [更新していた投稿]({origin_link})" if origin_link else "")
    )
    return {
        "title": title,
        "description": description,
        "color": _direction_color(direction),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Latest Tx: {fill.get('tx_hash') or 'N/A'}"},
    }


def _post(webhook, embed):
    """Return (outcome, message_id, retry_delay, channel_id)."""
    try:
        response = requests.post(
            retention.post_url(webhook),
            json={"username": "Hyperliquid Trade Monitor", "embeds": [embed],
                  "allowed_mentions": {"parse": []}},
            timeout=10,
        )
        if response.status_code == 429:
            return "retry", None, retention.retry_delay(response.json()), None
        if response.status_code >= 500:
            return "retry", None, 10.0, None
        response.raise_for_status()
        payload = response.json()
        message_id = payload.get("id") if isinstance(payload, dict) else None
        if not message_id:
            logger.error("Discord POST returned no message ID")
            return "unknown", None, 0, None
        channel_id = payload.get("channel_id")
        return (
            "ok", str(message_id), 0,
            str(channel_id) if channel_id and str(channel_id).isdigit() else None,
        )
    except requests.exceptions.ConnectTimeout:
        return "retry", None, 10.0, None
    except (requests.exceptions.RequestException, ValueError, TypeError) as error:
        logger.error("Discord POST outcome uncertain error_type=%s", type(error).__name__)
        return "unknown", None, 0, None


def _patch(webhook, message_id, embed):
    try:
        response = requests.patch(
            retention.message_url(webhook, message_id),
            json={"embeds": [embed], "allowed_mentions": {"parse": []}},
            timeout=10,
        )
        if response.status_code == 429:
            return "retry", retention.retry_delay(response.json())
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = None
            if retention.error_code(body) == 10008:
                return "missing", 0
        if response.status_code >= 500:
            return "retry", 10.0
        response.raise_for_status()
        return "ok", 0
    except requests.exceptions.ConnectTimeout:
        return "retry", 10.0
    except (requests.exceptions.RequestException, ValueError, TypeError) as error:
        logger.warning("Discord PATCH failed error_type=%s", type(error).__name__)
        return "retry", 10.0


def _recover_original_channel_id(webhook, message_id):
    """Recover metadata for sessions created before channel_id was stored."""
    if not retention.webhook_id(webhook) or not str(message_id).isdigit():
        return None
    try:
        response = requests.get(retention.message_url(webhook, message_id), timeout=10)
        if response.status_code != 200:
            return None
        payload = response.json()
        channel_id = payload.get("channel_id") if isinstance(payload, dict) else None
        return str(channel_id) if channel_id and str(channel_id).isdigit() else None
    except (requests.exceptions.RequestException, ValueError, TypeError):
        logger.warning("Could not resolve original Discord channel ID")
        return None


def _persist_message(db_path, message_id, webhook_url):
    # Uses the existing crash-recovery journal for Discord retention.
    retention.record_message(db_path, message_id, webhook_url, logger)


def _load_due(conn, now):
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT * FROM notification_sessions
        WHERE next_attempt_at <= ?
        ORDER BY last_event_at, coin
    """, (now,)).fetchall()


def _update(db_path, row, changes):
    columns = ", ".join(f"{column}=?" for column in changes)
    with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        # New events may increase event_count concurrently, so never overwrite
        # latest_fill or event_count after a network operation.
        conn.execute(
            f"UPDATE notification_sessions SET {columns} "
            "WHERE coin=? AND webhook_url=? AND message_id IS ?",
            (*changes.values(), row["coin"], row["webhook_url"], row["message_id"]),
        )


def _queue_completed_update(db_path, row):
    """Atomically close the active session and queue the original PATCH.

    New fills can start a fresh session immediately, even while the old
    completion PATCH is rate-limited or Discord is temporarily unavailable.
    """
    with closing(sqlite3.connect(db_path, timeout=10)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("""
            SELECT event_count, last_event_at, message_id
            FROM notification_sessions WHERE coin=? AND webhook_url=?
        """, (row["coin"], row["webhook_url"])).fetchone()
        if (
            current is None
            or current[0] != row["event_count"]
            or current[1] != row["last_event_at"]
            or current[2] != row["message_id"]
        ):
            conn.rollback()
            return False

        message_id = row["message_id"]
        if message_id and message_id != "__unknown__":
            conn.execute("""
                INSERT INTO notification_completions
                    (webhook_url, message_id, completed_embed)
                VALUES (?, ?, ?)
            """, (
                row["webhook_url"], message_id,
                json.dumps(_session_embed(row, completed=True), ensure_ascii=False),
            ))
        conn.execute("""
            DELETE FROM notification_sessions
            WHERE coin=? AND webhook_url=?
        """, (row["coin"], row["webhook_url"]))
        conn.commit()
        return True


def _dispatch_completed_updates(db_path, now):
    """Retry completion PATCH independently without reposting a final message."""
    with closing(sqlite3.connect(db_path, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        jobs = conn.execute("""
            SELECT * FROM notification_completions
            WHERE next_attempt_at <= ? ORDER BY id
        """, (now,)).fetchall()
    for job in jobs:
        outcome, delay = _patch(
            job["webhook_url"], job["message_id"],
            json.loads(job["completed_embed"]),
        )
        with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
            if outcome in ("ok", "missing"):
                conn.execute(
                    "DELETE FROM notification_completions WHERE id=?", (job["id"],),
                )
            else:
                conn.execute("""
                    UPDATE notification_completions
                    SET next_attempt_at=?
                    WHERE id=?
                """, (now + max(1, delay), job["id"]))


def dispatch_db(db_path, quiet_seconds, now=None):
    """Perform one delivery sweep outside the WebSocket callback."""
    now = time.time() if now is None else now
    with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        ensure_schema(conn)
        rows = _load_due(conn, now)
    for row in rows:
        webhook = row["webhook_url"]
        if not row["message_id"]:
            outcome, message_id, delay, channel_id = _post(webhook, json.loads(row["first_embed"]))
            if outcome == "ok":
                _persist_message(db_path, message_id, webhook)
                _update(db_path, row, {"message_id": message_id,
                                       "channel_id": channel_id,
                                       "updated_count": 1, "next_attempt_at": 0})
            elif outcome == "retry":
                _update(db_path, row, {"next_attempt_at": now + max(1, delay)})
            else:
                # A timed-out leading POST may have succeeded: never POST it again.
                # Keep the last fill to deliver a final POST after the quiet window.
                _update(db_path, row, {"message_id": "__unknown__",
                                       "updated_count": row["event_count"]})
            continue

        if row["message_id"] == "__unknown__":
            if now - row["last_event_at"] < quiet_seconds:
                continue
            # No confirmed message ID to edit. A trailing POST is the only
            # possible recovery and can be duplicated if its response is lost.
        elif row["updated_count"] < row["event_count"]:
            outcome, delay = _patch(webhook, row["message_id"], _session_embed(row))
            if outcome == "ok":
                _update(db_path, row, {"updated_count": row["event_count"],
                                       "next_attempt_at": 0})
            elif outcome == "missing":
                # The leading message could have been manually removed.
                _update(db_path, row, {"message_id": None,
                                       "first_embed": json.dumps(_session_embed(row)),
                                       "channel_id": None,
                                       "next_attempt_at": 0})
            else:
                _update(db_path, row, {"next_attempt_at": now + max(1, delay)})
            continue

        if now - row["last_event_at"] < quiet_seconds:
            continue

        # The initial POST supplies channel_id for new sessions. Existing rows
        # can recover it from the original webhook message without a bot token.
        channel_id = row["channel_id"]
        if not channel_id and row["message_id"] != "__unknown__":
            channel_id = _recover_original_channel_id(webhook, row["message_id"])
            if channel_id:
                _update(db_path, row, {"channel_id": channel_id})
        outcome, message_id, delay, _ = _post(
            webhook, _session_embed(row, final=True, channel_id=channel_id),
        )
        if outcome == "ok":
            _persist_message(db_path, message_id, webhook)
            _queue_completed_update(db_path, row)
        elif outcome == "retry":
            _update(db_path, row, {"next_attempt_at": now + max(1, delay)})
        else:
            # Discord may have accepted the final POST before losing its
            # response. Avoid an unbounded repeat, but finish the original.
            _queue_completed_update(db_path, row)

    _dispatch_completed_updates(db_path, now)


def dispatch_all(db_directory, quiet_seconds):
    for db_path in Path(db_directory).glob("trades_*.db"):
        try:
            dispatch_db(str(db_path), quiet_seconds)
        except (sqlite3.Error, OSError, ValueError):
            logger.exception("notification sweep failed db=%s", db_path.name)
