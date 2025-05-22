import sys
import os
import time
import signal
import atexit
import argparse
import subprocess
from hyperliquid_monitor.monitor import HyperliquidMonitor
from hyperliquid_monitor.types import Trade
from datetime import datetime
from collections import defaultdict
import requests
import json
from dotenv import load_dotenv

load_dotenv()
trade_cache = defaultdict(list)
monitor_instance = None

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

def process_trade(webhook_url: str, trade: Trade):
    global trade_cache

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
            send_to_discord(webhook_url, discord_msg)

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

def signal_handler(signum, frame):
    global monitor_instance
    print(f"Received signal {signum}, shutting down...")
    if monitor_instance:
        monitor_instance.stop()
    sys.exit(0)

def start_daemon(script_path, addresses_file):
    log_file = '/tmp/hyperliquid_monitor.log'
    error_file = '/tmp/hyperliquid_monitor_error.log'
    pidfile = '/tmp/hyperliquid_monitor.pid'
    
    remove_pidfile(pidfile)
    
    cmd = [sys.executable, script_path, addresses_file, '--background']
    
    print(f"Starting daemon with command: {' '.join(cmd)}")
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
            
            print(f"Daemon started with PID: {process.pid}")
            print(f"PID file: {pidfile}")
            
            time.sleep(2)
            if process.poll() is None:
                print("Daemon started successfully!")
                return True
            else:
                print("Daemon failed to start!")
                return False
                
    except Exception as e:
        print(f"Failed to start daemon: {e}")
        return False

def run_monitor(webhook_url: str, addresses_file: str, background_mode: bool = False):
    global monitor_instance
    
    if background_mode:
        log_file = '/tmp/hyperliquid_monitor.log'
        error_file = '/tmp/hyperliquid_monitor_error.log'
        pidfile = '/tmp/hyperliquid_monitor.pid'
        
        write_pidfile(pidfile)
        atexit.register(remove_pidfile, pidfile)
        
        with open(log_file, 'a') as f:
            f.write(f"\n=== Daemon started at {datetime.now()} ===\n")
            f.write(f"PID: {os.getpid()}\n")
            f.write(f"Addresses file: {addresses_file}\n")
            f.flush()
    
    try:
        addresses = load_addresses(addresses_file)
        print(f"Loaded {len(addresses)} addresses from {addresses_file}")

        monitor_instance = HyperliquidMonitor(
            addresses=addresses,
            db_path="trades.db",
            callback=lambda trade: process_trade(webhook_url, trade)
        )

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        print(f"Starting monitor for {len(addresses)} addresses...")
        print(f"Process PID: {os.getpid()}")
        
        monitor_instance.start()
        
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("Keyboard interrupt received, stopping...")
        if monitor_instance:
            monitor_instance.stop()
    except Exception as e:
        error_msg = f"Monitor error: {e}"
        print(error_msg)
        if monitor_instance:
            monitor_instance.stop()
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="Hyperliquid Trade Monitor",
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
        help="Run as a background daemon"
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

    if args.daemon:
        script_path = os.path.abspath(sys.argv[0])
        if start_daemon(script_path, args.addresses_file):
            print("Use the following commands to manage the daemon:")
            print(f"  Check status: ps aux | grep {os.path.basename(script_path)}")
            print("  Stop daemon: kill $(cat /tmp/hyperliquid_monitor.pid)")
            print("  View logs: tail -f /tmp/hyperliquid_monitor.log")
            print("  View errors: tail -f /tmp/hyperliquid_monitor_error.log")
        sys.exit(0)

    run_monitor(webhook_url, args.addresses_file, args.background)

if __name__ == "__main__":
    main()
