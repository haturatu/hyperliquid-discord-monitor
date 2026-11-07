import sys
import os
import time
import signal
import atexit
import argparse
import subprocess
import asyncio
import threading
import logging
import socket
from datetime import datetime, timezone
from collections import defaultdict
import requests
import json
import sqlite3
from pathlib import Path
from contextlib import suppress
from . import discord_retention as retention
from . import notification_lifecycle as lifecycle
from dotenv import load_dotenv
from hyperliquid.info import Info

from .addresses import load_addresses
from .models import Trade
from .ws_monitor import (
    HyperliquidUserFillsMonitor,
    MINIMAL_META,
    MINIMAL_SPOT_META,
    close_info,
    create_user_fills_info,
)

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
CONTAINER_ID = socket.gethostname()


class _MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level


_log_formatter = logging.Formatter(
    f"%(asctime)s %(levelname)-8s %(name)s container={CONTAINER_ID} pid=%(process)d %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setLevel(logging.DEBUG)
_stdout_handler.addFilter(_MaxLevelFilter(logging.WARNING))
_stdout_handler.setFormatter(_log_formatter)
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.WARNING)
_stderr_handler.setFormatter(_log_formatter)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    handlers=[_stdout_handler, _stderr_handler],
    force=True,
)
logger = logging.getLogger("hdm")

# .envから各種設定を読み込む
NOTIFICATION_SUPPRESSION_SECONDS = int(os.getenv('NOTIFICATION_SUPPRESSION_SECONDS', 600))
USER_FILL_INACTIVITY_RECONNECT_SECONDS = int(
    os.getenv(
        'USER_FILL_INACTIVITY_RECONNECT_SECONDS',
        os.getenv('WEBSOCKET_ACTIVITY_TIMEOUT', 900),
    )
) # 15分
DB_DIRECTORY = os.getenv('DB_DIRECTORY', '.') # デフォルトはカレントディレクトリ
HEALTHCHECK_FILE = os.getenv('HEALTHCHECK_FILE', '/tmp/healthcheck.txt')

# DB保存ディレクトリが存在しない場合は作成
if DB_DIRECTORY != '.':
    os.makedirs(DB_DIRECTORY, exist_ok=True)
    logger.debug("database directory set path=%s", DB_DIRECTORY)

trade_cache = defaultdict(list)
monitor_instances = {}
main_loop = None
monitor_tasks = {}
collateral_ticker_cache = {}
spot_meta_index_cache = None
shutdown_requested = threading.Event()

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_REQUEST_TIMEOUT = 10

processed_trades = set()


def user_fill_inactivity_timed_out(last_user_fill_time, timeout_seconds, now=None):
    if now is None:
        now = time.monotonic()
    return now - last_user_fill_time > timeout_seconds


def make_trade_uid(trade: Trade) -> str:
    timestamp = trade.timestamp.isoformat() if trade.timestamp else ""
    return "|".join([
        trade.address or "",
        trade.tx_hash or "",
        trade.coin or "",
        trade.direction or "",
        timestamp,
        f"{trade.price}",
        f"{trade.size}",
    ])

def touch_healthcheck_file():
    """ヘルスチェックファイルをtouch"""
    try:
        with open(HEALTHCHECK_FILE, 'a'):
            os.utime(HEALTHCHECK_FILE, None)
    except Exception as e:
        logger.warning("failed to touch healthcheck file path=%s error=%s", HEALTHCHECK_FILE, e)

def _discord_post_url(webhook_url: str) -> str:
    return retention.post_url(webhook_url)


def _discord_delete_url(webhook_url: str, message_id: str) -> str:
    return retention.message_url(webhook_url, message_id)


def record_discord_message(db_path: str, message_id: str, webhook_url: str) -> None:
    retention.record_message(db_path, message_id, webhook_url, logger)


def get_discord_meta(db_path: str, key: str):
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def set_discord_meta(db_path: str, key: str, value: str) -> None:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )
    except sqlite3.Error as e:
        logger.error("failed to write discord meta key=%s error=%s", key, e)


def _delete_discord_message(webhook_url: str, message_id: str) -> bool:
    for _ in range(3):
        try:
            response = requests.delete(
                _discord_delete_url(webhook_url, message_id), timeout=10
            )
            try:
                body = response.json()
            except ValueError:
                body = None
            if retention.deletion_confirmed(response.status_code, body):
                return True
            if response.status_code == 429:
                time.sleep(retention.retry_delay(body))
                continue
            logger.warning(
                "Discord delete deferred id=%s status=%s code=%s",
                message_id,
                response.status_code,
                retention.error_code(body),
            )
            if response.status_code < 500:
                return False
        except requests.exceptions.RequestException as error:
            logger.warning(
                "Discord delete deferred id=%s error_type=%s",
                message_id,
                type(error).__name__,
            )
        except (ValueError, TypeError):
            logger.warning("Invalid Discord retry response id=%s", message_id)
            return False
        time.sleep(1)
    return False


def cleanup_old_discord_messages(db_path: str, force: bool = False) -> None:
    keep = int(os.getenv("DISCORD_KEEP_MESSAGES", "50"))
    interval = float(os.getenv("DISCORD_CLEANUP_INTERVAL_HOURS", "24"))
    max_age = retention.max_age_hours(os.getenv("DISCORD_MESSAGE_MAX_AGE_HOURS", "0"))
    delete_interval = float(os.getenv("DISCORD_DELETE_INTERVAL_SECONDS", "0.5"))
    if max_age:
        interval = min(interval, 5 / 60)
    recovered = retention.recover_pending(db_path, logger)
    if not force and not retention.cleanup_due(
        get_discord_meta(db_path, "last_discord_cleanup"), interval
    ):
        return
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("""SELECT message_id, posted_at, webhook_url
                FROM discord_messages ORDER BY posted_at DESC, rowid DESC""").fetchall()
        with sqlite3.connect(db_path) as conn:
            active_ids = lifecycle.active_message_ids(conn)
        stale = [
            row for row in retention.stale_rows(rows, keep, max_age)
            if row[0] not in active_ids
        ]
        complete = recovered
        deleted = 0
        for message_id, posted_at, webhook_url in stale:
            if _delete_discord_message(webhook_url, message_id):
                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "DELETE FROM discord_messages WHERE message_id=?", (message_id,)
                    )
                deleted += 1
            else:
                complete = False
            time.sleep(delete_interval)
        if complete:
            set_discord_meta(
                db_path, "last_discord_cleanup", datetime.now(timezone.utc).isoformat()
            )
        logger.info(
            "Discord cleanup deleted=%s candidates=%s complete=%s",
            deleted,
            len(stale),
            complete,
        )
    except sqlite3.Error as error:
        logger.error("Discord cleanup DB failure error_type=%s", type(error).__name__)


def cleanup_discord_databases():
    # Include addresses removed from the subscription list and preview-only DBs.
    for path in Path(DB_DIRECTORY).glob("trades_*.db"):
        try:
            ensure_trades_table(str(path))
            cleanup_old_discord_messages(str(path))
        except Exception:
            logger.exception("Discord retention sweep failed db=%s", path.name)


async def discord_retention_loop():
    while not shutdown_requested.is_set():
        await asyncio.to_thread(cleanup_discord_databases)
        await asyncio.sleep(60)


def send_to_discord(webhook_url: str, message: str = None, embed: dict = None):
    payload = {"username": "Hyperliquid Trade Monitor"}
    if message:
        payload["content"] = message
    if embed:
        payload["embeds"] = [embed]
    for attempt in range(3):
        try:
            response = requests.post(
                _discord_post_url(webhook_url), json=payload, timeout=10
            )
            if response.status_code == 429:
                body = response.json()
                time.sleep(retention.retry_delay(body))
                continue
            response.raise_for_status()
            body = response.json()
            message_id = body.get("id") if isinstance(body, dict) else None
            if message_id:
                return str(message_id)
            logger.error(
                "Discord POST returned no message ID; delivery cannot be tracked"
            )
            return None
        except requests.exceptions.ConnectTimeout:
            # A connect timeout is safe to retry; a lost response is not.
            time.sleep(2**attempt)
        except requests.exceptions.RequestException as error:
            logger.error(
                "Discord send outcome uncertain; POST not retried error_type=%s",
                type(error).__name__,
            )
            return None
        except (ValueError, TypeError):
            logger.error("Discord POST returned an invalid message response")
            return None
    logger.error("Discord send exhausted retries")
    return None

def fetch_spot_meta_index_map():
    global spot_meta_index_cache
    if spot_meta_index_cache is not None:
        return spot_meta_index_cache

    try:
        response = requests.post(
            HYPERLIQUID_INFO_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"type": "spotMeta"}),
            timeout=HYPERLIQUID_REQUEST_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as e:
        logger.warning("failed to fetch spotMeta error=%s", e)
        spot_meta_index_cache = {}
        return spot_meta_index_cache

    index_map = {}
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    for item in tokens or []:
        if isinstance(item, dict) and "index" in item and "name" in item:
            index_map[item["index"]] = item["name"]

    spot_meta_index_cache = index_map
    return spot_meta_index_cache

def fetch_collateral_token_index(dex: str):
    try:
        response = requests.post(
            HYPERLIQUID_INFO_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"type": "metaAndAssetCtxs", "dex": dex}),
            timeout=HYPERLIQUID_REQUEST_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as e:
        logger.warning("failed to fetch metaAndAssetCtxs dex=%s error=%s", dex, e)
        return None

    if not payload or not isinstance(payload, list):
        return None
    meta = payload[0] if payload else None
    if not isinstance(meta, dict):
        return None
    return meta.get("collateralToken")

def get_collateral_ticker_for_dex(dex: str) -> str:
    if not dex:
        return "USDC"
    cached = collateral_ticker_cache.get(dex)
    if cached:
        return cached

    collateral_token = fetch_collateral_token_index(dex)
    if collateral_token is None:
        ticker = "USDC"
    elif isinstance(collateral_token, str):
        if collateral_token.isdigit():
            collateral_token = int(collateral_token)
        else:
            ticker = collateral_token
    elif isinstance(collateral_token, float) and collateral_token.is_integer():
        collateral_token = int(collateral_token)
    if "ticker" not in locals():
        if collateral_token == 0:
            ticker = "USDC"
        else:
            index_map = fetch_spot_meta_index_map()
            ticker = index_map.get(collateral_token, "USDC")

    collateral_ticker_cache[dex] = ticker
    return ticker

def build_ghost_url(trade: Trade) -> str:
    """Use the original Hyperliquid Ghost URL, including HIP-3 collateral."""
    dex = trade.coin.split(":", 1)[0] if trade.coin and ":" in trade.coin else ""
    collateral_ticker = get_collateral_ticker_for_dex(dex)
    return (
        f"https://app.hyperliquid.xyz/trade/{trade.coin}/{collateral_ticker}"
        f"?hloa={trade.address}"
    )


def build_trade_embed(trade: Trade, tag: str, ghost_url: str = None):
    direction = trade.direction or ""
    # Determine Color and Title based on trade
    color = 0x0099ff # Blue default
    title = f"New {trade.trade_type}"
    
    if "Long" in direction:
        color = 0x00ff00 # Green
        title = f"📈 {direction}"
    elif "Short" in direction:
        color = 0x0099ff # Blue (Short)
        title = f"📉 {direction}"
    
    if trade.closed_pnl:
        # Keep the PnL label, but use the position side for the card color.
        if trade.closed_pnl > 0:
            title = f"🟢 Closed Position (Profit)"
        else:
            title = f"🔴 Closed Position (Loss)"
    
    # Append Tag to Title if it exists
    if tag:
        title += f" (Tag: {tag})"

    # Reconstruct the original text block format
    # Tag is removed from here as it is now in the Title
    address_parts_text = []
    
    # Add "ポジションに変更があったよ！" at the top if trade type is FILL
    if trade.trade_type == "FILL":
        address_parts_text.append("ポジションに変更があったよ！")

    if ghost_url is None:
        ghost_url = build_ghost_url(trade)

    address_block_text = "\n".join(address_parts_text)

    display_direction = trade.direction or "Unknown"
    if trade.is_liquidation and trade.liquidation_kind:
        display_direction = f"{display_direction} / {trade.liquidation_kind}"
    original_format_text = f"""{address_block_text}
Coin: {trade.coin}
Price: {trade.price}
Direction: {display_direction}

Address: https://hypurrscan.io/address/{trade.address}
Ghost: {ghost_url}"""

    return {
        "title": title,
        "description": original_format_text, # No code block markdown
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {
            "text": f"Tx: {trade.tx_hash}"
        }
    }

def process_trade_with_db(webhook_url: str, trade: Trade, db_path: str, tag: str, specific_webhook_url: str = None):
    """Persist each distinct fill, then update its Discord notification session.

    A notification interval no longer drops fills: the initial event is POSTed,
    subsequent fills PATCH the same message, and an idle sweep POSTs the tail.
    """
    allowed_directions = {"Open Long", "Close Long", "Open Short", "Close Short"}
    if trade.direction not in allowed_directions:
        return

    trade_key = make_trade_uid(trade)
    if trade_key in processed_trades:
        return

    ensure_trades_table(db_path)
    if check_trade_exists_in_db(db_path, trade):
        processed_trades.add(trade_key)
        return

    if not trade.tx_hash:
        logger.error("cannot track fill without tx hash address_suffix=%s", trade.address[-8:])
        return
    try:
        ghost_url = build_ghost_url(trade)
        inserted = lifecycle.enqueue(
            db_path, trade, tag,
            (webhook_url, specific_webhook_url),
            first_embed=build_trade_embed(trade, tag, ghost_url=ghost_url),
            trade_uid=trade_key,
            ghost_url=ghost_url,
        )
    except sqlite3.Error:
        logger.exception("failed to persist fill and notification session tx=%s", trade.tx_hash)
        return
    processed_trades.add(trade_key)
    if not inserted:
        return
    logger.debug(
        "notification session updated address_suffix=%s coin=%s tx=%s",
        trade.address[-8:], trade.coin, trade.tx_hash,
    )


def ensure_trades_table(db_path: str) -> None:
    try:
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_uid TEXT,
                    tx_hash TEXT,
                    address TEXT NOT NULL,
                    coin TEXT,
                    direction TEXT,
                    timestamp TEXT,
                    seen_at INTEGER NOT NULL
                )
            """)

            columns_info = list(conn.execute("PRAGMA table_info(trades)"))
            columns = {row[1] for row in columns_info}
            tx_hash_is_primary = any(row[1] == "tx_hash" and row[5] == 1 for row in columns_info)

            if tx_hash_is_primary:
                conn.execute("ALTER TABLE trades RENAME TO trades_legacy")
                conn.execute("""
                    CREATE TABLE trades (
                        trade_uid TEXT,
                        tx_hash TEXT,
                        address TEXT NOT NULL,
                        coin TEXT,
                        direction TEXT,
                        timestamp TEXT,
                        seen_at INTEGER NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT OR IGNORE INTO trades (trade_uid, tx_hash, address, coin, direction, timestamp, seen_at)
                    SELECT
                        address || '|' || COALESCE(tx_hash, '') || '|' || COALESCE(coin, '') || '|' ||
                        COALESCE(direction, '') || '|' || COALESCE(timestamp, ''),
                        tx_hash,
                        address,
                        coin,
                        direction,
                        timestamp,
                        seen_at
                    FROM trades_legacy
                """)
                conn.execute("DROP TABLE trades_legacy")
                columns = {"trade_uid", "tx_hash", "address", "coin", "direction", "timestamp", "seen_at"}

            if "trade_uid" not in columns:
                conn.execute("ALTER TABLE trades ADD COLUMN trade_uid TEXT")

            conn.execute("""
                UPDATE trades
                SET trade_uid = COALESCE(
                    trade_uid,
                    address || '|' || COALESCE(tx_hash, '') || '|' || COALESCE(coin, '') || '|' ||
                    COALESCE(direction, '') || '|' || COALESCE(timestamp, '')
                )
                WHERE trade_uid IS NULL
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_trade_uid
                ON trades(trade_uid)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS discord_messages (
                    message_id TEXT PRIMARY KEY,
                    webhook_url TEXT NOT NULL,
                    posted_at TIMESTAMP NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
    except sqlite3.Error as e:
        logger.error("sqlite schema setup failed db=%s error=%s", os.path.abspath(db_path), e)

def record_trade_in_db(db_path: str, trade: Trade) -> None:
    if not trade.tx_hash:
        return False
    trade_uid = make_trade_uid(trade)
    try:
        ensure_trades_table(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO trades (trade_uid, tx_hash, address, coin, direction, timestamp, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_uid,
                    trade.tx_hash,
                    trade.address,
                    trade.coin,
                    trade.direction,
                    trade.timestamp.isoformat() if trade.timestamp else None,
                    int(time.time()),
                ),
            )
        return True
    except sqlite3.Error as e:
        logger.error("sqlite store trade failed db=%s error=%s", os.path.abspath(db_path), e)
        return False

def bootstrap_seen_fills(address: str, db_path: str) -> None:
    """
    Seed recent fills into DB once when DB is empty.
    This prevents replay notifications right after down/up.
    """
    ensure_trades_table(db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades")
            if cur.fetchone()[0] > 0:
                return
    except sqlite3.Error as e:
        logger.warning("bootstrap db inspect failed address_suffix=%s error=%s", address[-8:], e)
        return

    info = Info(skip_ws=True, meta=MINIMAL_META, spot_meta=MINIMAL_SPOT_META)
    try:
        fills = info.user_fills(address) or []
    except Exception as e:
        logger.warning("bootstrap user_fills failed address_suffix=%s error=%s", address[-8:], e)
        return
    finally:
        close_info(info)

    seeded = 0
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        tx_hash = fill.get("hash")
        if not tx_hash:
            continue
        trade = HyperliquidUserFillsMonitor.build_trade_from_fill(fill, address)
        if record_trade_in_db(db_path, trade):
            seeded += 1
    logger.info("bootstrap seeded fills address_suffix=%s count=%s", address[-8:], seeded)

def check_trade_exists_in_db(db_path: str, trade: Trade) -> bool:
    """DBに指定されたfill相当のトレードが既に存在するかチェック"""
    try:
        if not os.path.exists(db_path):
            return False
        ensure_trades_table(db_path)
        trade_uid = make_trade_uid(trade)
        timestamp = trade.timestamp.isoformat() if trade.timestamp else None
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1
                FROM trades
                WHERE trade_uid = ?
                   OR (
                        tx_hash = ?
                    AND address = ?
                    AND coin = ?
                    AND direction = ?
                    AND timestamp = ?
                   )
                LIMIT 1
                """,
                (
                    trade_uid,
                    trade.tx_hash,
                    trade.address,
                    trade.coin,
                    trade.direction,
                    timestamp,
                ),
            )
            row = cursor.fetchone()
            return row is not None
    except sqlite3.Error as e:
        logger.error("sqlite check trade failed error=%s", e)
        return False
    except Exception as e:
        logger.error("check trade failed error=%s", e)
        return False

def run_test_preview(
    addresses_file: str,
    enable_posting: bool = False,
    timeout_seconds: int = None,
    max_entries: int = None,
):
    if not os.path.exists(addresses_file):
        logger.error("addresses file not found path=%s", addresses_file)
        return

    addresses = load_addresses(addresses_file)
    timeout_seconds = int(
        timeout_seconds
        if timeout_seconds is not None
        else os.getenv("TEST_PREVIEW_TIMEOUT_SECONDS", 60)
    )
    max_entries = int(
        max_entries
        if max_entries is not None
        else os.getenv("TEST_PREVIEW_MAX_ENTRIES", 10)
    )
    count = 0
    seen_hashes = set()
    pending = set(addresses.keys())
    printed_per_address = defaultdict(int)
    count_lock = threading.Lock()
    done_event = threading.Event()

    logger.info(
        "starting websocket preview addresses=%s timeout_seconds=%s",
        len(addresses),
        timeout_seconds,
    )
    if enable_posting:
        logger.warning("discord posting is enabled for preview")
    logger.info("preview max entries=%s", max_entries)

    def callback(msg):
        nonlocal count
        if not isinstance(msg, dict):
            return
        data = msg.get("data") or {}
        fills = data.get("fills") or []
        address = (data.get("user") or "").lower()
        if address not in addresses:
            return
        for fill in fills:
            tx_hash = fill.get("hash")
            if not tx_hash:
                continue
            with count_lock:
                if tx_hash in seen_hashes:
                    continue
                if count >= max_entries:
                    done_event.set()
                    return
                if pending and address not in pending:
                    continue
                seen_hashes.add(tx_hash)
                count += 1
                printed_per_address[address] += 1
                if printed_per_address[address] >= 1 and address in pending:
                    pending.remove(address)
            trade = HyperliquidUserFillsMonitor.build_trade_from_fill(fill, address)
            info = addresses.get(trade.address)
            tag = info.get("tag") if info else None
            specific_webhook = info.get("webhook") if info else None
            embed = build_trade_embed(trade, tag)
            print("\n-----")
            print(f"Title: {embed['title']}")
            print(embed["description"])
            print(embed["footer"]["text"])

            if enable_posting:
                webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
                if webhook_url:
                    logger.info("preview sending to global webhook")
                    message_id = send_to_discord(webhook_url, embed=embed)
                    if message_id:
                        preview_db = os.path.join(DB_DIRECTORY, "trades_preview.db")
                        ensure_trades_table(preview_db)
                        record_discord_message(preview_db, message_id, webhook_url)
                if specific_webhook:
                    logger.info("preview sending to specific webhook")
                    message_id = send_to_discord(specific_webhook, embed=embed)
                    if message_id:
                        preview_db = os.path.join(DB_DIRECTORY, "trades_preview.db")
                        ensure_trades_table(preview_db)
                        record_discord_message(preview_db, message_id, specific_webhook)

            if count >= max_entries or not pending:
                done_event.set()
                return

    info = create_user_fills_info()
    for address in addresses.keys():
        info.subscribe({"type": "userFills", "user": address}, callback)

    done_event.wait(timeout_seconds)
    try:
        close_info(info)
    except Exception as e:
        logger.warning("error closing preview info error=%s", e)

    if enable_posting:
        cleanup_discord_databases()

    with count_lock:
        printed = count
    if printed < max_entries:
        logger.info(
            "preview timeout reached printed=%s max_entries=%s", printed, max_entries
        )

def write_pidfile(pidfile):
    try:
        with open(pidfile, 'w') as f:
            f.write(str(os.getpid()))
        logger.info("pid file created path=%s", pidfile)
    except IOError as e:
        logger.error("failed to write pidfile path=%s error=%s", pidfile, e)

def remove_pidfile(pidfile):
    try:
        if os.path.exists(pidfile):
            os.remove(pidfile)
            logger.info("pid file removed path=%s", pidfile)
    except OSError as e:
        logger.warning("error removing pidfile path=%s error=%s", pidfile, e)

async def monitor_addresses_async(webhook_url: str, addresses: dict):
    """複数アドレスを単一 WebSocket 接続で監視し、切断時に自動再接続する"""
    global monitor_instances

    # A restart must not inherit a stale heartbeat from a previous process.
    try:
        os.unlink(HEALTHCHECK_FILE)
    except FileNotFoundError:
        pass
    except OSError as error:
        logger.warning("failed to clear previous healthcheck heartbeat error=%s", error)

    address_state = {}
    for address, info in addresses.items():
        db_path = os.path.join(DB_DIRECTORY, f"trades_{address[-8:]}.db")
        ensure_trades_table(db_path)
        bootstrap_seen_fills(address, db_path)
        address_state[address] = {
            'tag': info.get('tag'),
            'webhook': info.get('webhook'),
            'db_path': db_path,
        }

    shared_state = {
        'last_user_fill_time': time.monotonic(),
        'connection_dead': threading.Event()
    }
    reconnect_attempts = 0

    def monitor_callback(trade):
        shared_state['last_user_fill_time'] = time.monotonic()
        address_info = address_state.get(trade.address)
        if not address_info:
            return
        return process_trade_with_db(
            webhook_url,
            trade,
            address_info['db_path'],
            address_info['tag'],
            address_info['webhook'],
        )

    while not shutdown_requested.is_set():
        monitor = None
        monitor_thread = None
        shared_state['connection_dead'].clear()
        shared_state['last_user_fill_time'] = time.monotonic()

        try:
            logger.info("initializing shared monitor addresses=%s", len(addresses))
            for index, (address, info) in enumerate(addresses.items()):
                tag = info.get('tag')
                logger.info("preparing subscription index=%s address=%s tag=%s", index, address, tag or "")

            monitor = HyperliquidUserFillsMonitor(
                addresses=list(addresses.keys()),
                callback=monitor_callback
            )
            monitor_instances['shared'] = monitor

            try:
                if hasattr(monitor, 'info') and hasattr(monitor.info, 'ws_manager') and monitor.info.ws_manager:
                    ws_manager = monitor.info.ws_manager
                    original_on_close = ws_manager.ws.on_close
                    original_on_error = ws_manager.ws.on_error

                    def on_ws_close(ws, close_status_code, close_msg):
                        logger.warning("shared websocket closed code=%s message=%s", close_status_code, close_msg)
                        shared_state['connection_dead'].set()
                        if original_on_close:
                            original_on_close(ws, close_status_code, close_msg)

                    def on_ws_error(ws, error):
                        logger.warning("shared websocket error error=%s", error)
                        shared_state['connection_dead'].set()
                        if original_on_error:
                            original_on_error(ws, error)

                    ws_manager.ws.on_close = on_ws_close
                    ws_manager.ws.on_error = on_ws_error
                    logger.info("installed shared websocket close/error handlers")
                else:
                    logger.warning("could not find shared websocket manager for close/error handlers")
            except Exception as e:
                logger.warning("failed to install websocket close/error handlers error=%s", e)

            error_container = {'error': None}

            def start_monitor_thread():
                """A thread to run the blocking monitor.start() call."""
                try:
                    logger.info("starting shared monitor thread")
                    monitor.start()
                except Exception as e:
                    error_container['error'] = e
                    logger.error("shared monitor thread failed error=%s", e)
                finally:
                    logger.info("shared monitor thread finished")

            monitor_thread = threading.Thread(target=start_monitor_thread, daemon=True)
            monitor_thread.start()

            await asyncio.sleep(2)
            if error_container['error']:
                raise error_container['error']

            logger.info("shared monitor started addresses=%s", len(addresses))
            reconnect_attempts = 0

            while monitor_thread.is_alive() and not shutdown_requested.is_set():
                if shared_state['connection_dead'].is_set():
                    logger.warning("dead shared websocket detected; reconnecting")
                    if monitor:
                        monitor.stop()
                    break

                if user_fill_inactivity_timed_out(
                    shared_state['last_user_fill_time'],
                    USER_FILL_INACTIVITY_RECONNECT_SECONDS,
                    time.monotonic(),
                ):
                    logger.warning(
                        "user fill inactivity timeout seconds=%s; reconnecting",
                        USER_FILL_INACTIVITY_RECONNECT_SECONDS,
                    )
                    shared_state['connection_dead'].set()
                    if monitor:
                        monitor.stop()
                    break

                # Health reflects the monitor loop, not whether a wallet traded.
                touch_healthcheck_file()
                await asyncio.sleep(10)

            if shutdown_requested.is_set():
                logger.info("shutdown requested; shared monitor loop stopping")
            elif error_container['error']:
                logger.warning("shared monitor stopped with error; reconnecting error=%s", error_container['error'])
            else:
                logger.warning("shared monitor stopped; reconnecting")

        except Exception as e:
            logger.exception("shared monitor loop failed error=%s", e)

        finally:
            if 'shared' in monitor_instances:
                try:
                    logger.info("cleaning up shared monitor instance")
                    monitor_instances['shared'].stop()
                except Exception as e:
                    logger.warning("error stopping shared monitor during cleanup error=%s", e)
                del monitor_instances['shared']

            if shutdown_requested.is_set():
                logger.info("shutdown requested; skipping reconnect wait")
            else:
                wait_time = min(30 * (2 ** min(reconnect_attempts, 3)), 300)
                reconnect_attempts += 1
                logger.info(
                    "waiting before reconnect seconds=%s next_attempt=%s",
                    wait_time,
                    reconnect_attempts,
                )
                await asyncio.sleep(wait_time)

async def discord_notification_loop():
    """Flush live PATCH updates and trailing POSTs independently of new fills."""
    while not shutdown_requested.is_set():
        await asyncio.to_thread(
            lifecycle.dispatch_all, DB_DIRECTORY, NOTIFICATION_SUPPRESSION_SECONDS
        )
        await asyncio.sleep(2)


async def run_multi_monitor_async(webhook_url: str, addresses: dict):
    """複数アドレスの非同期監視"""
    logger.info("starting multi-address monitor addresses=%s", len(addresses))

    for i, (address, info) in enumerate(addresses.items()):
        tag = info.get("tag")
        logger.info(
            "configured address index=%s address=%s tag=%s", i, address, tag or ""
        )

    retention_task = asyncio.create_task(discord_retention_loop())
    notification_task = asyncio.create_task(discord_notification_loop())
    try:
        await monitor_addresses_async(webhook_url, addresses)
    except Exception as e:
        logger.exception("multi-monitor failed error=%s", e)
        raise
    finally:
        retention_task.cancel()
        notification_task.cancel()
        with suppress(asyncio.CancelledError):
            await retention_task
        with suppress(asyncio.CancelledError):
            await notification_task

def signal_handler(signum, frame):
    global monitor_instances, main_loop
    shutdown_requested.set()
    logger.info("received signal; shutting down signal=%s", signum)

    # すべての監視インスタンスを停止
    for address, monitor in monitor_instances.items():
        try:
            logger.info("stopping monitor key=%s", address)
            monitor.stop()
        except Exception as e:
            logger.warning("error stopping monitor key=%s error=%s", address, e)
    
    monitor_instances.clear()
    
    if main_loop and main_loop.is_running():
        main_loop.stop()
    
    sys.exit(0)

def start_daemon(script_path, addresses_file):
    """単一プロセスでのデーモン起動"""
    log_file = '/tmp/hyperliquid_monitor_multi.log'
    error_file = '/tmp/hyperliquid_monitor_multi_error.log'
    pidfile = '/tmp/hyperliquid_monitor_multi.pid'
    
    remove_pidfile(pidfile)
    
    cmd = [sys.executable, script_path, 'monitor', addresses_file, '--background']
    
    logger.info("starting daemon command=%s", " ".join(cmd))
    logger.info("daemon log file path=%s", log_file)
    logger.info("daemon error log file path=%s", error_file)
    
    try:
        with open(log_file, 'a') as log_f, open(error_file, 'a') as err_f:
            process = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                start_new_session=True, 
                cwd=os.getcwd()
            )
            
            with open(pidfile, 'w') as f:
                f.write(str(process.pid))
            
            logger.info("daemon process started pid=%s pidfile=%s", process.pid, pidfile)
            
            time.sleep(2)
            if process.poll() is None:
                logger.info("daemon started successfully")
                logger.info("check status command=ps aux | grep %s", os.path.basename(script_path))
                logger.info("stop daemon command=kill $(cat %s)", pidfile)
                logger.info("view logs command=tail -f %s", log_file)
                logger.info("view errors command=tail -f %s", error_file)
                return True
            else:
                logger.error("daemon failed to start")
                return False
                
    except Exception as e:
        logger.exception("failed to start daemon error=%s", e)
        return False

def run_monitor(webhook_url: str, addresses_file: str, background_mode: bool = False):
    """メイン監視ループ（複数アドレス対応）"""
    global main_loop
    shutdown_requested.clear()
    
    addresses = load_addresses(addresses_file)
    
    logger.info("loaded addresses count=%s", len(addresses))
    for i, (addr, info) in enumerate(addresses.items()):
        tag = info.get('tag')
        logger.info("loaded address index=%s address=%s tag=%s", i + 1, addr, tag or "")
    
    # シグナルハンドラーの設定
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    logger.info("process started pid=%s", os.getpid())
    
    try:
        # 新しいイベントループを作成して実行
        if sys.version_info >= (3, 7):
            asyncio.run(run_multi_monitor_async(webhook_url, addresses))
        else:
            # Python 3.6以下の場合
            loop = asyncio.get_event_loop()
            main_loop = loop
            loop.run_until_complete(run_multi_monitor_async(webhook_url, addresses))
            
    except KeyboardInterrupt:
        logger.info("keyboard interrupt received; stopping")
    except Exception as e:
        error_msg = f"Multi-monitor error: {e}"
        logger.exception(error_msg)
        sys.exit(1)
    finally:
        # クリーンアップ
        for address, monitor in monitor_instances.items():
            try:
                monitor.stop()
            except Exception:
                logger.exception("monitor cleanup failed address=%s", address)
        monitor_instances.clear()

def build_parser():
    parser = argparse.ArgumentParser(
        description="Hyperliquid Trade Monitor (Multi-Address WebSocket Support)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command")

    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Run trade monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    monitor_parser.add_argument(
        "addresses_file",
        nargs="?",
        default="addresses.txt",
        help="Path to addresses file"
    )
    monitor_parser.add_argument(
        "-d", "--daemon",
        action="store_true",
        help="Run as background daemon (single process monitoring all addresses)"
    )
    monitor_parser.add_argument(
        "--background",
        action="store_true",
        help="Internal flag used by daemon child process"
    )
    monitor_parser.add_argument(
        "--webhook-url",
        help="Override DISCORD_WEBHOOK_URL environment variable"
    )

    tests_parser = subparsers.add_parser(
        "tests",
        help="Preview recent fills without running full monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    tests_parser.add_argument(
        "-f", "--addresses-file",
        default="addresses.txt",
        help="Path to addresses file used in preview"
    )
    tests_parser.add_argument(
        "--post",
        action="store_true",
        help="Also post preview messages to Discord"
    )
    tests_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("TEST_PREVIEW_TIMEOUT_SECONDS", 60)),
        help="Seconds to wait before stopping preview"
    )
    tests_parser.add_argument(
        "--max-entries",
        type=int,
        default=int(os.getenv("TEST_PREVIEW_MAX_ENTRIES", 10)),
        help="Maximum fills to print before stopping preview"
    )
    tests_parser.add_argument(
        "legacy_action",
        nargs="?",
        choices=["post"],
        help="Legacy positional mode (equivalent to --post)"
    )
    return parser

def parse_args():
    parser = build_parser()
    argv = sys.argv[1:]

    # Backward compatibility: `python script.py addresses.txt`
    if argv and argv[0] not in {"monitor", "tests"} and not argv[0].startswith("-"):
        argv = ["monitor", *argv]

    if not argv:
        parser.print_help()
        sys.exit(0)

    return parser.parse_args(argv)

def main():
    args = parse_args()

    if args.command == "tests":
        enable_posting = args.post or args.legacy_action == "post"
        run_test_preview(
            args.addresses_file,
            enable_posting=enable_posting,
            timeout_seconds=args.timeout_seconds,
            max_entries=args.max_entries
        )
        sys.exit(0)

    addresses_file = args.addresses_file
    webhook_url = args.webhook_url or os.getenv('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        logger.error("DISCORD_WEBHOOK_URL not found in environment")
        logger.error("create a .env file with DISCORD_WEBHOOK_URL=your_webhook_url")
        sys.exit(1)

    if not os.path.exists(addresses_file):
        logger.error("addresses file not found path=%s", addresses_file)
        sys.exit(1)

    if args.daemon and not args.background:
        script_path = os.path.abspath(sys.argv[0])
        addresses = load_addresses(addresses_file)
        
        logger.info("starting daemon for addresses=%s", len(addresses))
        start_daemon(script_path, addresses_file)
        sys.exit(0)

    run_monitor(webhook_url, addresses_file, args.background)

if __name__ == "__main__":
    main()
