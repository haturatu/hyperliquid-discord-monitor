import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import cli_app as app


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("DISCORD_KEEP_MESSAGES", "1")
    monkeypatch.setenv("DISCORD_DELETE_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("DISCORD_MESSAGE_MAX_AGE_HOURS", "0")
    monkeypatch.setattr(app.time, "sleep", lambda *a: None)
    path = tmp_path / "trades_test.db"
    app.ensure_trades_table(str(path))
    now = datetime.now(timezone.utc)
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO discord_messages VALUES (?,?,?)",
            [
                (
                    "100",
                    "https://discord.invalid/api/webhooks/222/old",
                    (now - timedelta(hours=25)).isoformat(),
                ),
                (
                    "101",
                    "https://discord.invalid/api/webhooks/333/new",
                    now.isoformat(),
                ),
            ],
        )
    return path


def ids(database):
    with sqlite3.connect(database) as db:
        return [
            row[0]
            for row in db.execute(
                "SELECT message_id FROM discord_messages ORDER BY message_id"
            )
        ]


@pytest.mark.parametrize(
    "status,body", [(404, {"code": 10015}), (404, None), (503, None)]
)
def test_delete_failures_keep_ids_and_retry(database, monkeypatch, status, body):
    response = Mock(status_code=status)
    response.json.return_value = body
    delete = Mock(return_value=response)
    monkeypatch.setattr(app.requests, "delete", delete)
    app.cleanup_old_discord_messages(str(database))
    first = delete.call_count
    assert ids(database) == ["100", "101"]
    assert app.get_discord_meta(str(database), "last_discord_cleanup") is None
    app.cleanup_old_discord_messages(str(database))
    assert delete.call_count > first


def test_expiry_cleans_below_count_limit(database, monkeypatch):
    monkeypatch.setenv("DISCORD_KEEP_MESSAGES", "50")
    monkeypatch.setenv("DISCORD_MESSAGE_MAX_AGE_HOURS", "24")
    response = Mock(status_code=204)
    response.json.side_effect = ValueError("empty response")
    monkeypatch.setattr(app.requests, "delete", Mock(return_value=response))
    app.cleanup_old_discord_messages(str(database))
    assert ids(database) == ["101"]


def test_sweep_includes_removed_address_and_preview_databases(
    database, tmp_path, monkeypatch
):
    preview = tmp_path / "trades_preview.db"
    app.ensure_trades_table(str(preview))
    cleanup = Mock()
    monkeypatch.setattr(app, "cleanup_old_discord_messages", cleanup)
    app.cleanup_discord_databases()
    assert {Path(call.args[0]).name for call in cleanup.call_args_list} == {
        "trades_test.db",
        "trades_preview.db",
    }


def test_ambiguous_post_timeout_is_not_retried(monkeypatch):
    post = Mock(
        side_effect=requests.exceptions.ReadTimeout("accepted but response lost")
    )
    monkeypatch.setattr(app.requests, "post", post)
    assert (
        app.send_to_discord(
            "https://discord.invalid/api/webhooks/222/token", message="test"
        )
        is None
    )
    assert post.call_count == 1


def test_preview_posts_are_recorded(database, tmp_path, monkeypatch, capsys):
    address = "0x" + "a" * 40
    marker = tmp_path / "addresses.txt"
    marker.write_text(address)
    monkeypatch.setattr(app, "load_addresses", lambda p: {address: {}})

    class Info:
        def subscribe(self, subscription, callback):
            callback({"data": {"user": address, "fills": [{"hash": "test-hash"}]}})

    monkeypatch.setattr(app, "create_user_fills_info", Info)
    monkeypatch.setattr(app, "close_info", lambda info: None)
    monkeypatch.setattr(
        app.HyperliquidUserFillsMonitor,
        "build_trade_from_fill",
        lambda f, a: SimpleNamespace(address=a),
    )
    monkeypatch.setattr(
        app,
        "build_trade_embed",
        lambda *a: {"title": "test", "description": "test", "footer": {"text": "test"}},
    )
    monkeypatch.setattr(app, "send_to_discord", lambda *a, **kw: "999")
    monkeypatch.setenv(
        "DISCORD_WEBHOOK_URL", "https://discord.invalid/api/webhooks/222/token"
    )
    monkeypatch.setattr(app, "cleanup_discord_databases", Mock())
    app.run_test_preview(str(marker), True, 1, 1)
    assert ids(tmp_path / "trades_preview.db") == ["999"]
