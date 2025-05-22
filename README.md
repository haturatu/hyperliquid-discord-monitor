# hyperliquid-discord-monitor
## Install

### Prerequisites
- Python 3.7+
- pip package manager

### Installation Steps

1. Clone or download the project files
2. Install required dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file in the project directory:
```bash
touch .env
```

4. Add your Discord webhook URL to the `.env` file:
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your/webhook/url
```

5. Create an `addresses` file containing wallet addresses to monitor (one per line):
```bash
touch addresses
```

## Usage

### Basic Usage
Monitor addresses from the default `addresses` file:
```bash
python hyperliquid-discord-monitor.py addresses
```

Monitor addresses from a custom file:
```bash
python hyperliquid-discord-monitor.py custom_addresses.txt
```

### Daemon Mode
Run the monitor as a background daemon:
```bash
python hyperliquid-discord-monitor.py addresses -d
```

Or with a custom addresses file:
```bash
python hyperliquid-discord-monitor.py custom_addresses.txt -d
```

### Daemon Management Commands
When running in daemon mode, use these commands to manage the process:

Check if daemon is running:
```bash
ps aux | grep hyperliquid-discord-monitor.py
```

Stop the daemon:
```bash
kill $(cat /tmp/hyperliquid_monitor.pid)
```

View logs:
```bash
tail -f /tmp/hyperliquid_monitor.log
```

View error logs:
```bash
tail -f /tmp/hyperliquid_monitor_error.log
```

## Example

### Setup Example

1. Create your environment file:
```bash
echo "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/abcdefg" > .env
```

2. Add wallet addresses to monitor:
```bash
cat > addresses << EOF
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12
0x9876543210fedcba9876543210fedcba98765432
EOF
```

3. Start monitoring:
```bash
python hyperliquid-discord-monitor.py addresses
```

### Discord Message Example
When a trade is detected, you'll receive Discord messages like:
```
**[2024-01-15 14:30:25] New FILL**
Address: https://hypurrscan.io/address/0x1234567890abcdef1234567890abcdef12345678
Trade Tx hash: https://hypurrscan.io/tx/0xabcdef...

Coin: ETH
Price: 2450.50
Direction: Long
PnL: 🟢 125.75
Hash: 0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890ab
```

### File Structure
```
project/
├── hyperliquid-discord-monitor.py
├── .env
├── addresses
├── trades.db (created automatically)
└── README.md
```

### Environment Variables
- `DISCORD_WEBHOOK_URL`: Your Discord webhook URL (required)

### Address File Format
The addresses file should contain one Ethereum address per line:
```
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12
0x9876543210fedcba9876543210fedcba98765432
```

Empty lines are ignored, so you can add spacing for better readability.
