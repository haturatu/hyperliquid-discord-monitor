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

### Recommendation for Daemonization
If you daemonize the process directly, it may go into a sleep state.
Therefore, we recommend using Supervisord for proper process daemonization.

example conf:
```bash
$ cat /etc//etc/supervisor/conf.d/hyperliquid-discord-monitor.conf
[program:hyperliquid-discord-monitor]
command=python3 hyperliquid-discord-monitor.py addresses
user=darkstar
directory=/home/$USER/git/hyperliquid-discord-monitor
autostart=true
autorestart=true
stderr_logfile=/var/log/h-monitor.log
stderr_logfile_maxbytes=1MB
stdout_logfile=/var/log/h-monitor.out.log
stdout_logfile_maxbytes=1MB
stdout_logfile_backups=0
stderr_logfile_backups=0
environment=PATH="/home/$USER/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/home/$USER/.$USER/bin:/home/$USER/.cargo/bin:/home/$USER/.npm-global/bin",PYTHONPATH="/home/$USER/.local/lib/python3.11/site-packages",HOME="/home/$USER"
```
```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start hyperliquid-discord-monitor
```
