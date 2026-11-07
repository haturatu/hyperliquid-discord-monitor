import importlib
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ("ncmma" if (ROOT / "ncmma").is_dir() else "src")
sys.path.insert(0, str(SOURCE))
store = importlib.import_module("discord_retention")


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "messages.db"
    with sqlite3.connect(path) as conn:
        store.ensure_schema(conn)
    return path


def test_migration_preserves_legacy_ids_without_guessing_webhook(database):
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE discord_messages")
        conn.execute(
            "CREATE TABLE discord_messages(message_id TEXT PRIMARY KEY, posted_at TEXT)"
        )
        conn.execute("INSERT INTO discord_messages VALUES ('100', '2020-01-01')")
        store.ensure_schema(conn)
        store.ensure_schema(conn)
        assert conn.execute("SELECT * FROM discord_messages").fetchall() == [
            ("100", "2020-01-01", None)
        ]


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (204, None, True),
        (404, {"code": 10008}, True),
        (404, {"code": 10015}, False),
        (404, None, False),
        (403, {"code": 50013}, False),
        (503, None, False),
        (200, {}, False),
    ],
)
def test_only_confirmed_deletion_discards_id(status, body, expected):
    assert store.deletion_confirmed(status, body) is expected


def test_optional_expiry_applies_even_below_keep_limit():
    now = datetime.now(timezone.utc)
    rows = [
        ("101", now.isoformat(), "route"),
        ("100", (now - timedelta(hours=25)).isoformat(), "route"),
    ]
    assert store.stale_rows(rows, 50) == []
    assert store.stale_rows(rows, 50, 24) == [rows[1]]
    assert store.stale_rows(rows, 1) == [rows[1]]


def test_timestamp_gate_handles_old_naive_and_new_aware_values():
    assert store.cleanup_due("2020-01-01T00:00:00", 24)
    assert not store.cleanup_due(datetime.now(timezone.utc).isoformat(), 24)
    assert store.cleanup_due("invalid", 24)


def test_wait_false_is_replaced_and_thread_is_kept():
    url = "https://discord.invalid/api/webhooks/222/token?wait=false&thread_id=444"
    assert store.post_url(url).count("wait=") == 1
    assert "wait=true" in store.post_url(url)
    assert store.message_url(url, "100").endswith("/messages/100?thread_id=444")


def test_legacy_route_requires_matching_webhook_and_message():
    url = "https://discord.invalid/api/webhooks/222/token"
    assert store.legacy_message_matches({"id": "100", "webhook_id": "222"}, "100", url)
    assert not store.legacy_message_matches(
        {"id": "100", "webhook_id": "333"}, "100", url
    )
    assert not store.legacy_message_matches(
        {"id": "101", "webhook_id": "222"}, "100", url
    )


def test_db_failure_journals_id_and_recovery_is_idempotent(database, monkeypatch):
    logger = logging.getLogger("test")
    original = store._insert

    def fail(*args):
        raise sqlite3.OperationalError("simulated locked DB")

    monkeypatch.setattr(store, "_insert", fail)
    store.record_message(
        database, "100", "https://discord.invalid/api/webhooks/222/token", logger
    )
    journals = list(database.parent.glob("*.discord-pending/*.json"))
    assert len(journals) == 1
    assert journals[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(journals[0].read_text())["message_id"] == "100"
    monkeypatch.setattr(store, "_insert", original)
    assert store.recover_pending(database, logger)
    assert store.recover_pending(database, logger)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT message_id FROM discord_messages").fetchall() == [
            ("100",)
        ]
    assert not list(database.parent.glob("*.discord-pending/*.json"))


def test_both_storage_failures_are_not_reported_as_success(database, monkeypatch):
    def fail_file(*args, **kwargs):
        raise PermissionError("simulated unwritable journal")

    def fail_db(*args):
        raise sqlite3.OperationalError("simulated DB failure")

    monkeypatch.setattr(store.tempfile, "NamedTemporaryFile", fail_file)
    monkeypatch.setattr(store, "_insert", fail_db)
    with pytest.raises(RuntimeError, match="could not be persisted"):
        store.record_message(database, "100", "route", logging.getLogger("test"))


def test_logging_routes_warnings_to_stderr(capsys):
    logger = logging.getLogger("retention-stream-test")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handlers = store.console_handlers(logging.Formatter("%(message)s"))
    for handler in handlers:
        logger.addHandler(handler)
    try:
        logger.debug("debug-marker")
        logger.info("info-marker")
        logger.warning("warning-marker")
        logger.error("error-marker")
        output = capsys.readouterr()
        assert "debug-marker" in output.out and "info-marker" in output.out
        assert "warning-marker" not in output.out and "error-marker" not in output.out
        assert "warning-marker" in output.err and "error-marker" in output.err
    finally:
        for handler in handlers:
            logger.removeHandler(handler)


def test_count_retention_compares_instants_not_iso_strings():
    rows = [
        ("100", "2026-01-01T09:00:00+00:00", "route"),
        ("101", "2026-01-01T00:00:00-10:00", "route"),
    ]
    assert store.stale_rows(rows, 1) == [rows[0]]


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_invalid_expiry_is_rejected(value):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        store.max_age_hours(value)
