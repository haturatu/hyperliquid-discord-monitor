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

5. Create an `addresses.txt` file containing wallet addresses to monitor (one per line):
```bash
touch addresses.txt
```

## Running with Docker (Recommended)

Using Docker is recommended as it automates dependency and process management.

### 1. Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

### 2. Configuration

In the project's root directory, you need to prepare the following three items.

#### a) `addresses.txt` File

Write the Hyperliquid addresses to monitor, one per line.

**Example of `addresses.txt`:**
```
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12
```

#### b) `data` Directory

This is used to permanently store the trade history database (SQLite file). It prevents data loss even if the container is restarted.

Create it with the following command:
```bash
mkdir data
```

#### c) `.env` File

Define the environment variables required for the application.

- `DISCORD_WEBHOOK_URL`: **(Required)** Your Discord Webhook URL.
- `DB_DIRECTORY`: **(Required for Docker)** Set this to `/app/data` to save the database in a persistent volume.
- `NOTIFICATION_SUPPRESSION_SECONDS`: (Optional) Cooldown time in seconds between notifications for the same type of trade. Default is `60`.

**Example of `.env`:**
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your_webhook_id/your_webhook_token
NOTIFICATION_SUPPRESSION_SECONDS=300
DB_DIRECTORY=/app/data
```

### 3. Running the Service

Once the configuration is complete, manage the service with the following commands.

- **Start the service in the background:**
  ```bash
  docker-compose up -d
  ```

- **View logs in real-time:**
  ```bash
  docker-compose logs -f
  ```
  *(Pressing `Ctrl+C` will exit the log view, but the service will continue to run.)*

- **Stop the service:**
  ```bash
  docker-compose down
  ```

---

## Usage

### Basic Usage
Monitor addresses from the default `addresses.txt` file:
```bash
python hyperliquid-discord-monitor.py addresses.txt
```

Monitor addresses from a custom file:
```bash
python hyperliquid-discord-monitor.py custom_addresses.txt
```

### Daemon Mode
Run the monitor as a background daemon:
```bash
python hyperliquid-discord-monitor.py addresses.txt -d
```

Or with a custom addresses file:
```bash
python hyperliquid-discord-monitor.py custom_addresses.txt -d
```

## Example

### Setup Example

1. Create your environment file:
```bash
echo "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/abcdefg" > .env
```

2. Add wallet addresses to monitor:
```bash
cat > addresses.txt << EOF
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12
0x9876543210fedcba9876543210fedcba98765432
EOF
```

python hyperliquid-discord-monitor.py addresses.txt

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
├── addresses.txt
├── trades.db (created automatically)
└── README.md
```

### Environment Variables
- `DISCORD_WEBHOOK_URL`: Your Discord webhook URL (required)

### Address File Format
The `addresses.txt` file should contain one Ethereum address per line:
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
$ cat /etc/supervisor/conf.d/hyperliquid-discord-monitor.conf
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
