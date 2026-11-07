import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from src import notification_lifecycle as lifecycle


def trade(direction="Open Long", tx="tx-1"):
    return SimpleNamespace(
        address="0x" + "1" * 40, coin="BTC", direction=direction,
        price=100.0, size=1.0, tx_hash=tx,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_leading_patch_and_trailing(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_11111111.db")
    sent, edited = [], []

    def post(url, embed):
        sent.append((url, embed))
        return "ok", str(len(sent)), 0, "123456789"

    def patch(url, message_id, embed):
        edited.append((url, message_id, embed))
        return "ok", 0

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_patch", patch)
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)

    lifecycle.enqueue(db, trade(), "trader", ["webhook-a", "webhook-a"],
                      {"title": "Open Long"}, now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    assert len(sent) == 1
    assert not edited

    lifecycle.enqueue(db, trade("Close Long", "tx-2"), "trader", ["webhook-a"],
                      {"title": "Close Long"}, now=110)
    lifecycle.dispatch_db(db, 60, now=111)
    assert len(sent) == 1
    assert len(edited) == 1
    assert edited[0][1] == "1"
    assert "2回" in edited[0][2]["description"]
    assert "Close Long" in edited[0][2]["description"]

    lifecycle.dispatch_db(db, 60, now=169)
    assert len(sent) == 1
    lifecycle.dispatch_db(db, 60, now=170)
    assert len(sent) == 2
    assert "変更完了" in sent[-1][1]["title"]
    assert "Close Long" in sent[-1][1]["description"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_sessions").fetchone()[0] == 0


def test_session_survives_restart_and_protects_active_message(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_22222222.db")
    sent = []

    def post(url, embed):
        sent.append(url)
        return "ok", str(len(sent)), 0, "123456789"

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_patch", lambda *_: ("ok", 0))
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    lifecycle.enqueue(db, trade(), None, ["one", "two"], {"title": "A"}, now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    with sqlite3.connect(db) as conn:
        assert lifecycle.active_message_ids(conn) == {"1", "2"}

    # No more fills arrive: the independent sweep still emits trailing POSTs.
    lifecycle.dispatch_db(db, 60, now=160)
    assert sent == ["one", "two", "one", "two"]
    with sqlite3.connect(db) as conn:
        assert lifecycle.active_message_ids(conn) == set()


def test_discord_retry_does_not_lose_last_event(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_33333333.db")
    responses = iter([("ok", "1", 0, "123456789"), ("retry", None, 5, None), ("ok", "2", 0, "123456789")])
    monkeypatch.setattr(lifecycle, "_post", lambda *_: next(responses))
    monkeypatch.setattr(lifecycle, "_patch", lambda *_: ("ok", 0))
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    lifecycle.enqueue(db, trade(), None, ["one"], {"title": "A"}, now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    lifecycle.dispatch_db(db, 60, now=160)
    with sqlite3.connect(db) as conn:
        due = conn.execute("SELECT next_attempt_at FROM notification_sessions").fetchone()[0]
    assert due == 165
    lifecycle.dispatch_db(db, 60, now=165)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_sessions").fetchone()[0] == 0


def test_unknown_leading_post_is_not_replayed(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_44444444.db")
    sent = []

    def post(url, embed):
        sent.append(embed["title"])
        if len(sent) == 1:
            return "unknown", None, 0, None
        return "ok", "999", 0, "123456789"

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    lifecycle.enqueue(db, trade(), None, ["one"], now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    assert sent == ["📈 BTC Open Long"]
    lifecycle.dispatch_db(db, 60, now=105)
    assert len(sent) == 1
    lifecycle.dispatch_db(db, 60, now=160)
    assert len(sent) == 2
    assert "変更完了" in sent[-1]


def test_patch_missing_restarts_leading_message(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_55555555.db")
    sent = []
    monkeypatch.setattr(lifecycle, "_post", lambda url, embed: (
        sent.append(embed["title"]) or "ok", str(len(sent)), 0, "123456789"
    ))
    monkeypatch.setattr(lifecycle, "_patch", lambda *_: ("missing", 0))
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    lifecycle.enqueue(db, trade(), None, ["one"], now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    lifecycle.enqueue(db, trade("Close Long", "tx-2"), None, ["one"], now=110)
    lifecycle.dispatch_db(db, 60, now=111)
    lifecycle.dispatch_db(db, 60, now=112)
    assert len(sent) == 2
    assert "更新中" in sent[-1]


def test_trade_and_notification_are_one_transaction(tmp_path):
    db = str(tmp_path / "trades_66666666.db")
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE trades (
            trade_uid TEXT UNIQUE, tx_hash TEXT, address TEXT, coin TEXT,
            direction TEXT, timestamp TEXT, seen_at INTEGER
        )""")
    args = (db, trade(), None, ["one"])
    assert lifecycle.enqueue(*args, now=100, trade_uid="unique-fill") is True
    assert lifecycle.enqueue(*args, now=120, trade_uid="unique-fill") is False
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
        count = conn.execute("SELECT event_count FROM notification_sessions").fetchone()[0]
        assert count == 1


def test_ghost_link_present_in_initial_patch_and_final(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_77777777.db")
    entry = trade()
    entry.coin = "xyz:BTC"
    # HIP-3 must preserve the metadata-resolved collateral rather than USDC.
    ghost = (
        f"https://app.hyperliquid.xyz/trade/xyz:BTC/USDT"
        f"?hloa={entry.address}"
    )
    original_embed = {
        "title": "📈 Open Long",
        "description": f"Coin: xyz:BTC\\nGhost: {ghost}",
    }
    posted, patched = [], []

    def post(url, embed):
        posted.append(embed)
        return "ok", str(len(posted)), 0, "123456789"

    def patch(url, message_id, embed):
        patched.append(embed)
        return "ok", 0

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_patch", patch)
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)

    lifecycle.enqueue(
        db, entry, "test", ["webhook-a"], first_embed=original_embed,
        ghost_url=ghost, now=100,
    )
    lifecycle.dispatch_db(db, 60, now=101)
    assert f"Ghost: {ghost}" in posted[0]["description"]

    more = trade("Close Long", "tx-2")
    more.coin = entry.coin
    lifecycle.enqueue(
        db, more, "test", ["webhook-a"],
        ghost_url=ghost, now=110,
    )
    lifecycle.dispatch_db(db, 60, now=111)
    assert f"Ghost: {ghost}" in patched[0]["description"]

    lifecycle.dispatch_db(db, 60, now=170)
    assert f"Ghost: {ghost}" in posted[-1]["description"]
    assert "変更完了" in posted[-1]["title"]


def test_regular_market_ghost_url_fallback(tmp_path):
    db = str(tmp_path / "trades_88888888.db")
    entry = trade()
    lifecycle.enqueue(db, entry, None, ["webhook-a"], now=100)
    with sqlite3.connect(db) as conn:
        first_embed, latest_fill = conn.execute(
            "SELECT first_embed, latest_fill FROM notification_sessions"
        ).fetchone()

    import json
    expected = (
        f"https://app.hyperliquid.xyz/trade/BTC/USDC"
        f"?hloa={entry.address}"
    )
    assert f"Ghost: {expected}" in json.loads(first_embed)["description"]
    assert json.loads(latest_fill)["ghost_url"] == expected


def test_hip3_ghost_url_respects_collateral_metadata(monkeypatch):
    from src import cli_app
    queried = []
    def resolve(dex):
        queried.append(dex)
        return "USDT"
    monkeypatch.setattr(cli_app, "get_collateral_ticker_for_dex", resolve)
    entry = trade()
    entry.coin = "xyz:BTC"
    expected = (
        f"https://app.hyperliquid.xyz/trade/xyz:BTC/USDT"
        f"?hloa={entry.address}"
    )
    assert cli_app.build_ghost_url(entry) == expected
    assert queried == ["xyz"]



def test_weighted_average_and_completed_original(tmp_path, monkeypatch):
    """The original PATCH thread closes with VWAP; the final POST stays unchanged."""
    db = str(tmp_path / "trades_weighted.db")
    posted, patched = [], []
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)

    def post(url, embed):
        posted.append(embed)
        return "ok", str(len(posted)), 0, "123456789"

    def patch(url, msg_id, embed):
        patched.append((msg_id, embed))
        return "ok", 0

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_patch", patch)

    first = trade()
    first.size, first.price = 2.0, 100.0
    second = trade("Close Long", "tx-2")
    second.size, second.price = 1.0, 130.0

    lifecycle.enqueue(db, first, "team", ["one"], now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    lifecycle.enqueue(db, second, "team", ["one"], now=110)
    lifecycle.dispatch_db(db, 60, now=111)
    assert "更新中" in patched[-1][1]["title"]
    assert "平均約定価格: 110" in patched[-1][1]["description"]

    lifecycle.dispatch_db(db, 60, now=170)
    assert len(posted) == 2
    assert "ポジション変更完了" in posted[-1]["title"]
    assert "最終約定価格: 130" in posted[-1]["description"]
    assert "平均約定価格" not in posted[-1]["description"]

    assert patched[-1][0] == "1"
    assert "✅" in patched[-1][1]["title"]
    assert "更新完了" in patched[-1][1]["title"]
    assert "更新中" not in patched[-1][1]["title"]
    assert "平均約定価格: 110" in patched[-1][1]["description"]
    assert "Ghost:" in patched[-1][1]["description"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_sessions").fetchone()[0] == 0


def test_completed_patch_retry_never_reposts_final(tmp_path, monkeypatch):
    """After final POST succeeds, a failed completion PATCH is retried by ID."""
    db = str(tmp_path / "trades_retry_completion.db")
    posted, patched = [], []
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)

    def post(url, embed):
        posted.append(embed)
        return "ok", str(len(posted)), 0, "123456789"

    def patch(url, msg_id, embed):
        patched.append(embed)
        if len(patched) == 1:
            return "retry", 5
        return "ok", 0

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_patch", patch)
    lifecycle.enqueue(db, trade(), None, ["one"], now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    lifecycle.dispatch_db(db, 60, now=160)
    assert len(posted) == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_sessions").fetchone()[0] == 0
        message_id, next_attempt = conn.execute(
            "SELECT message_id, next_attempt_at FROM notification_completions"
        ).fetchone()
        assert message_id == "1"
        assert next_attempt == 165

    # Simulate a process restart; only the original PATCH should be retried.
    lifecycle.dispatch_db(db, 60, now=165)
    assert len(posted) == 2
    assert len(patched) == 2
    assert "更新完了" in patched[-1]["title"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_completions").fetchone()[0] == 0


def test_existing_session_migrates_without_fabricated_average(tmp_path):
    db = str(tmp_path / "old_sessions.db")
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE notification_sessions (
            coin TEXT NOT NULL,
            webhook_url TEXT NOT NULL,
            first_embed TEXT NOT NULL,
            latest_fill TEXT NOT NULL,
            tag TEXT,
            event_count INTEGER NOT NULL,
            updated_count INTEGER NOT NULL DEFAULT 0,
            message_id TEXT,
            last_event_at REAL NOT NULL,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (coin, webhook_url)
        )""")
        conn.execute("""INSERT INTO notification_sessions (
            coin, webhook_url, first_embed, latest_fill,
            event_count, message_id, last_event_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("BTC", "one", "{}", '{"address": "0x1", "coin": "BTC", "price": 130}',
             3, "7", 100))
        lifecycle.ensure_schema(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM notification_sessions").fetchone()
        embed = lifecycle._session_embed(row, completed=True)
        assert "集計不可" in embed["description"]
        assert "更新完了" in embed["title"]
        assert "最終約定価格: 130" in lifecycle._session_embed(row, final=True)["description"]



def test_new_fill_creates_new_session_while_completed_patch_is_queued(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_new_after_final.db")
    posted = []
    patches = []

    def post(url, embed):
        posted.append(embed)
        return "ok", str(len(posted)), 0, "123456789"

    def patch(url, msg_id, embed):
        patches.append((msg_id, embed))
        if len(patches) == 1:
            return "retry", 30
        return "ok", 0

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_patch", patch)
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)

    lifecycle.enqueue(db, trade(), None, ["one"], now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    lifecycle.dispatch_db(db, 60, now=160)
    assert len(posted) == 2
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_completions").fetchone()[0] == 1

    fresh = trade("Open Short", "tx-after-final")
    lifecycle.enqueue(db, fresh, None, ["one"], now=170)
    lifecycle.dispatch_db(db, 60, now=171)
    assert len(posted) == 3
    assert "Open Short" in posted[-1]["title"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_sessions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM notification_completions").fetchone()[0] == 1

    lifecycle.dispatch_db(db, 60, now=190)
    assert patches[-1][0] == "1"
    assert "更新完了" in patches[-1][1]["title"]
    assert len(posted) == 3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_sessions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM notification_completions").fetchone()[0] == 0



def test_direction_color_stays_green_long_blue_short_when_complete(tmp_path, monkeypatch):
    """A final POST and the completed original keep the latest side's color."""
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    post_embeds, patched = [], []

    def post(url, embed):
        post_embeds.append((url, embed))
        # Each destination may point to a different channel.
        channel_id = "987654321" if url == "long-webhook" else "123456789"
        return "ok", str(1000 + len(post_embeds)), 0, channel_id

    def patch(url, message_id, embed):
        patched.append((url, message_id, embed))
        return "ok", 0

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_patch", patch)

    for side, webhook, color in (
        ("Open Long", "long-webhook", 0x00FF00),
        ("Open Short", "short-webhook", 0x0099FF),
    ):
        db = str(tmp_path / ("trades_" + side.replace(" ", "_") + ".db"))
        event = trade(side, "tx-" + side)
        lifecycle.enqueue(db, event, None, [webhook], now=100)
        lifecycle.dispatch_db(db, 60, now=101)
        original_id = str(1000 + len(post_embeds))
        assert post_embeds[-1][1]["color"] == color

        update = trade("Close " + side.split()[-1], "update-" + side)
        lifecycle.enqueue(db, update, None, [webhook], now=110)
        lifecycle.dispatch_db(db, 60, now=111)
        assert patched[-1][2]["color"] == color

        lifecycle.dispatch_db(db, 60, now=170)
        finished = post_embeds[-1][1]
        completed_original = patched[-1][2]
        assert "ポジション変更完了" in finished["title"]
        assert finished["color"] == color
        assert "更新完了" in completed_original["title"]
        assert completed_original["color"] == color
        channel_id = "987654321" if webhook == "long-webhook" else "123456789"
        expected = f"https://discord.com/channels/@me/{channel_id}/{original_id}"
        assert f"[更新していた投稿]({expected})" in finished["description"]
        assert "投稿元" not in completed_original["description"]


def test_legacy_session_resolves_original_channel_before_final(tmp_path, monkeypatch):
    """Older SQLite sessions without channel_id can still link their source."""
    db = str(tmp_path / "trades_legacy.db")
    posted = []
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    monkeypatch.setattr(lifecycle, "_post", lambda _, embed: (
        posted.append(embed) or "ok", str(len(posted)), 0, None
    ))
    monkeypatch.setattr(lifecycle, "_patch", lambda *_: ("ok", 0))
    lifecycle.enqueue(db, trade(), None, ["https://discord.com/api/webhooks/11/token"], now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    assert "投稿元" not in posted[0]["description"]

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE notification_sessions SET channel_id=NULL")
    monkeypatch.setattr(lifecycle, "_recover_original_channel_id", lambda *_: "5555555")
    lifecycle.dispatch_db(db, 60, now=160)
    assert (
        "[更新していた投稿](https://discord.com/channels/@me/5555555/1)"
        in posted[-1]["description"]
    )


def test_post_response_carries_channel_id(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "12345", "channel_id": "98765"}

    def post(url, json, timeout):
        assert "wait=true" in url
        assert timeout == 10
        return Response()

    monkeypatch.setattr(lifecycle.requests, "post", post)
    assert lifecycle._post("https://discord.com/api/webhooks/1/token", {"title": "test"}) == (
        "ok", "12345", 0, "98765"
    )


def test_short_color_for_initial_embed():
    assert lifecycle._initial_embed(trade("Open Short"), None)["color"] == 0x0099FF
    assert lifecycle._initial_embed(trade("Open Long"), None)["color"] == 0x00FF00
