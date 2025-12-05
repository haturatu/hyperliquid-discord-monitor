import sys
import os
import time
import signal
import atexit
import argparse
import subprocess
import asyncio
import threading
from hyperliquid_monitor.monitor import HyperliquidMonitor
from hyperliquid_monitor.types import Trade
from datetime import datetime
from collections import defaultdict
import requests
import json
from dotenv import load_dotenv

load_dotenv()

# .envから各種設定を読み込む
NOTIFICATION_SUPPRESSION_SECONDS = int(os.getenv('NOTIFICATION_SUPPRESSION_SECONDS', 60))
DB_DIRECTORY = os.getenv('DB_DIRECTORY', '.') # デフォルトはカレントディレクトリ

# DB保存ディレクトリが存在しない場合は作成
if DB_DIRECTORY != '.':
    os.makedirs(DB_DIRECTORY, exist_ok=True)
    print(f"Database directory set to: {DB_DIRECTORY}")

last_notification_time = defaultdict(float)

trade_cache = defaultdict(list)
monitor_instances = {}
main_loop = None
monitor_tasks = {}

processed_trades = set()
startup_grace_period = {}

original_signal = signal.signal

def patched_signal(sig, handler):
    """スレッド内でのシグナル設定を無効化"""
    if threading.current_thread() != threading.main_thread():
        # メインスレッド以外では何もしない
        return None
    return original_signal(sig, handler)

signal.signal = patched_signal

def send_to_discord(webhook_url: str, message: str):
    payload = {
        "content": message,
        "username": "Hyperliquid Trade Monitor"
    }
    headers = {
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers=headers
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"Failed to send message to Discord: {e}\n")

def process_trade_with_db(webhook_url: str, trade: Trade, db_path: str):
    """DBパスを指定してトレードを処理"""
    global trade_cache, processed_trades, startup_grace_period, last_notification_time

    address_suffix = trade.address[-8:]
    trade_key = f"{trade.address}:{trade.tx_hash}"
    
    # メモリベースの重複チェック
    if trade_key in processed_trades:
        print(f"[{address_suffix}] Trade {trade.tx_hash} already processed in memory, skipping")
        return
    
    # 通知を抑制する（起動時の大量通知を防ぐ）
    current_time = time.time()
    address_startup_time = startup_grace_period.get(trade.address)
    
    if address_startup_time and (current_time - address_startup_time) < 60:
        print(f"[{address_suffix}] Startup grace period - skipping historical trade: {trade.tx_hash}")
        processed_trades.add(trade_key)
        return
    
    if os.path.exists(db_path) and check_trade_exists_in_db(db_path, trade.tx_hash):
        print(f"[{address_suffix}] Trade {trade.tx_hash} already exists in DB, skipping notification")
        processed_trades.add(trade_key)
        return

    # 通知抑制ロジック
    suppression_key = (trade.address, trade.coin, trade.direction)
    last_time = last_notification_time.get(suppression_key)

    if last_time and (current_time - last_time) < NOTIFICATION_SUPPRESSION_SECONDS:
        print(f"[{address_suffix}] Notification for {trade.coin} {trade.direction} suppressed. Last notification was at {datetime.fromtimestamp(last_time).strftime('%Y-%m-%d %H:%M:%S')}")
        processed_trades.add(trade_key)
        return

    # 新しいトレードとして処理
    processed_trades.add(trade_key)
    
    trade_cache[trade.tx_hash].append(trade)
    trades = trade_cache[trade.tx_hash]
    total_size = sum(t.size for t in trades)
    timestamp = trade.timestamp.strftime('%Y-%m-%d %H:%M:%S')

    discord_msg = ""

    if len(trades) == 1:
        discord_msg = f"""**[{timestamp}] New {trade.trade_type}**
Address: https://hypurrscan.io/address/{trade.address}
Trade Tx hash: https://hypurrscan.io/tx/{trade.tx_hash}
```
Coin: {trade.coin}
Price: {trade.price}"""
        
        if trade.trade_type == "FILL":
            discord_msg += f"\nDirection: {trade.direction}"
        
        if trade.closed_pnl:
            pnl_emoji = "🟢" if trade.closed_pnl > 0 else "🔴"
            discord_msg += f"\nPnL: {pnl_emoji} {trade.closed_pnl:.2f}"
        
        discord_msg += f"\nHash: {trade.tx_hash}\n```"

        if discord_msg:
            print(f"[{address_suffix}] Sending Discord notification for new trade: {trade.tx_hash}")
            send_to_discord(webhook_url, discord_msg)
            # 通知を送信したら、時刻を更新
            last_notification_time[suppression_key] = current_time

def check_trade_exists_in_db(db_path: str, tx_hash: str) -> bool:
    """DBに指定されたtx_hashのトレードが既に存在するかチェック"""
    import sqlite3
    try:
        # DBファイルが存在しない場合は存在しないと判定
        if not os.path.exists(db_path):
            return False
            
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # テーブルの存在確認
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='trades'
        """)
        
        if not cursor.fetchone():
            conn.close()
            return False  # ログ出力を削除（起動時の大量出力を防ぐ）
        
        # tx_hash列の存在確認
        cursor.execute("PRAGMA table_info(trades)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'tx_hash' not in columns:
            conn.close()
            return False
        
        # tradesテーブルでtx_hashをチェック
        cursor.execute("SELECT COUNT(*) FROM trades WHERE tx_hash = ?", (tx_hash,))
        count = cursor.fetchone()[0]
        
        conn.close()
        return count > 0
        
    except sqlite3.Error as e:
        print(f"SQLite error checking trade in DB: {e}")
        return False
    except Exception as e:
        print(f"Error checking trade in DB: {e}")
        return False

def load_addresses(file_path: str) -> list:
    addresses = []
    try:
        with open(file_path, 'r') as f:
            for line in f:
                address = line.strip()
                if address:  # Skip empty lines
                    addresses.append(address)
    except IOError as e:
        sys.stderr.write(f"Error reading addresses file: {e}\n")
        sys.exit(1)

    if not addresses:
        sys.stderr.write("No addresses found in addresses file\n")
        sys.exit(1)

    return addresses

def write_pidfile(pidfile):
    try:
        with open(pidfile, 'w') as f:
            f.write(str(os.getpid()))
        print(f"PID file created: {pidfile}")
    except IOError as e:
        sys.stderr.write(f"Failed to write pidfile: {e}\n")

def remove_pidfile(pidfile):
    try:
        if os.path.exists(pidfile):
            os.remove(pidfile)
            print(f"PID file removed: {pidfile}")
    except OSError as e:
        print(f"Error removing pidfile: {e}")

async def monitor_address_async(webhook_url: str, address: str, address_index: int):
    """非同期で単一アドレスを監視"""
    global startup_grace_period
    
    reconnect_interval = 3600  # 1時間ごとに再接続
    last_reconnect_time = time.time()
    
    # DBパスを環境変数で指定されたディレクトリ内に作成
    db_path = os.path.join(DB_DIRECTORY, f"trades_{address[-8:]}.db")
    
    def create_callback(addr, db_file):
        def callback(trade):
            return process_trade_with_db(webhook_url, trade, db_file)
        return callback
    
    while True:
        monitor = None
        try:
            print(f"[{address_index}] Starting monitor for address: {address}")
            
            startup_grace_period[address] = time.time()
            
            monitor = HyperliquidMonitor(
                addresses=[address],
                db_path=db_path,
                callback=create_callback(address, db_path)
            )
            
            monitor_instances[address] = monitor
            
            # 監視開始を別スレッドで実行
            start_event = threading.Event()
            error_container = {'error': None}
            
            def start_monitor():
                try:
                    monitor.start()
                    start_event.set()
                except Exception as e:
                    error_container['error'] = e
                    start_event.set()
            
            monitor_thread = threading.Thread(target=start_monitor, daemon=True)
            monitor_thread.start()
            
            while not start_event.is_set():
                await asyncio.sleep(0.1)
            
            if error_container['error']:
                raise error_container['error']
            
            print(f"[{address_index}] Monitor started at {datetime.now()}")
            print(f"[{address_index}] Grace period active for 60 seconds (historical trades will be ignored)")
            
            while True:
                await asyncio.sleep(60)  # 1分ごとにチェック
                
                now = time.time()
                if now - last_reconnect_time >= reconnect_interval:
                    print(f"[{address_index}] Reconnecting WebSocket for {address}...")
                    
                    # 古い監視を停止
                    if address in monitor_instances:
                        try:
                            monitor_instances[address].stop()
                        except Exception as e:
                            print(f"[{address_index}] Error stopping monitor: {e}")
                        del monitor_instances[address]
                    
                    last_reconnect_time = now
                    break
                    
        except Exception as e:
            print(f"[{address_index}] Monitor error for {address}: {e}")
            if address in monitor_instances:
                try:
                    monitor_instances[address].stop()
                except:
                    pass
                del monitor_instances[address]
            
            # エラー後の待機時間
            await asyncio.sleep(30)

async def run_multi_monitor_async(webhook_url: str, addresses: list):
    """複数アドレスの非同期監視"""
    print(f"Starting multi-address monitor for {len(addresses)} addresses")
    
    # 各アドレスの監視タスクを作成
    tasks = []
    for i, address in enumerate(addresses):
        task = asyncio.create_task(
            monitor_address_async(webhook_url, address, i)
        )
        tasks.append(task)
        monitor_tasks[address] = task
        print(f"Created monitoring task for address {i}: {address}")
    
    try:
        # すべてのタスクを並行実行
        await asyncio.gather(*tasks)
    except Exception as e:
        print(f"Error in multi-monitor: {e}")
        raise

def signal_handler(signum, frame):
    global monitor_instances, main_loop
    print(f"Received signal {signum}, shutting down...")
    
    # パッチを元に戻す
    signal.signal = original_signal
    
    # すべての監視インスタンスを停止
    for address, monitor in monitor_instances.items():
        try:
            print(f"Stopping monitor for {address}")
            monitor.stop()
        except Exception as e:
            print(f"Error stopping monitor for {address}: {e}")
    
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
    
    cmd = [sys.executable, script_path, addresses_file, '--background']
    
    print(f"Starting multi-address daemon with command: {' '.join(cmd)}")
    print(f"Logs will be written to: {log_file}")
    print(f"Errors will be written to: {error_file}")
    
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
            
            print(f"Multi-address daemon started with PID: {process.pid}")
            print(f"PID file: {pidfile}")
            
            time.sleep(2)
            if process.poll() is None:
                print("Multi-address daemon started successfully!")
                print("\nManagement commands:")
                print(f"  Check status: ps aux | grep {os.path.basename(script_path)}")
                print(f"  Stop daemon: kill $(cat {pidfile})")
                print(f"  View logs: tail -f {log_file}")
                print(f"  View errors: tail -f {error_file}")
                return True
            else:
                print("Multi-address daemon failed to start!")
                return False
                
    except Exception as e:
        print(f"Failed to start multi-address daemon: {e}")
        return False

def run_monitor(webhook_url: str, addresses_file: str, background_mode: bool = False):
    """メイン監視ループ（複数アドレス対応）"""
    global main_loop
    
    addresses = load_addresses(addresses_file)
    
    print(f"Loading {len(addresses)} addresses:")
    for i, addr in enumerate(addresses):
        print(f"  {i+1}: {addr}")
    
    # シグナルハンドラーの設定
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    print(f"Process PID: {os.getpid()}")
    
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
        print("Keyboard interrupt received, stopping...")
    except Exception as e:
        error_msg = f"Multi-monitor error: {e}"
        print(error_msg)
        sys.exit(1)
    finally:
        # クリーンアップ
        for address, monitor in monitor_instances.items():
            try:
                monitor.stop()
            except:
                pass
        monitor_instances.clear()

def main():
    parser = argparse.ArgumentParser(
        description="Hyperliquid Trade Monitor (Multi-Address WebSocket Support)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "addresses_file",
        nargs="?",
        help="Path to the file containing addresses to monitor"
    )
    parser.add_argument(
        "-d", "--daemon",
        action="store_true",
        help="Run as background daemon (single process monitoring all addresses)"
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help=argparse.SUPPRESS
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        sys.stderr.write("Error: DISCORD_WEBHOOK_URL not found in environment variables.\n")
        sys.stderr.write("Please create a .env file with DISCORD_WEBHOOK_URL=your_webhook_url\n")
        sys.exit(1)

    if not os.path.exists(args.addresses_file):
        sys.stderr.write(f"Addresses file not found: {args.addresses_file}\n")
        sys.exit(1)

    if args.daemon and not args.background:
        script_path = os.path.abspath(sys.argv[0])
        addresses = load_addresses(args.addresses_file)
        
        print(f"Starting daemon for {len(addresses)} addresses in single process")
        start_daemon(script_path, args.addresses_file)
        sys.exit(0)

    run_monitor(webhook_url, args.addresses_file, args.background)

if __name__ == "__main__":
    main()
