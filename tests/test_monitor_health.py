"""Monitor health must not depend on user fills arriving."""
import asyncio
import os
import threading
from types import SimpleNamespace

from src import cli_app


def test_monitor_heartbeat_without_trades(tmp_path, monkeypatch):
    address = "0x" + "1" * 40
    heartbeat = tmp_path / "healthcheck.txt"
    heartbeat.write_text("stale")
    os.utime(heartbeat, (1, 1))
    stop = threading.Event()

    class FakeMonitor:
        def __init__(self, addresses, callback):
            self.addresses = addresses
            self.info = SimpleNamespace()
            self.finished = threading.Event()

        def start(self):
            self.finished.wait(5)

        def stop(self):
            self.finished.set()

    monkeypatch.setattr(cli_app, "HEALTHCHECK_FILE", str(heartbeat))
    monkeypatch.setattr(cli_app, "shutdown_requested", stop)
    monkeypatch.setattr(cli_app, "HyperliquidUserFillsMonitor", FakeMonitor)
    monkeypatch.setattr(cli_app, "ensure_trades_table", lambda *_: None)
    monkeypatch.setattr(cli_app, "bootstrap_seen_fills", lambda *_: None)

    real_sleep = asyncio.sleep

    async def no_wait(seconds):
        if seconds == 10:
            stop.set()
        await real_sleep(0.01)

    monkeypatch.setattr(cli_app.asyncio, "sleep", no_wait)
    asyncio.run(cli_app.monitor_addresses_async(
        "https://example.invalid/webhook",
        {address: {"tag": "test", "webhook": None}},
    ))
    assert heartbeat.is_file()
    assert heartbeat.stat().st_mtime > 1
