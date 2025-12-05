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

## Dockerでの実行 (推奨)

Dockerを使用すると、依存関係やプロセスの管理が自動化されるため、この方法を推奨します。

### 1. 前提条件

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

### 2. 設定

プロジェクトのルートディレクトリで、以下の3つの準備が必要です。

#### a) `addresses.txt` ファイル

監視対象のHyperliquidアドレスを1行に1つずつ記述します。

**`addresses.txt`の例:**
```
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12
```

#### b) `data` ディレクトリ

取引履歴のデータベース (SQLiteファイル) を永続的に保存するために使用します。コンテナを再起動してもデータが失われるのを防ぎます。

以下のコマンドで作成してください:
```bash
mkdir data
```

#### c) `.env` ファイル

アプリケーションに必要な環境変数を記述します。

- `DISCORD_WEBHOOK_URL`: **(必須)** あなたのDiscord Webhook URL。
- `DB_DIRECTORY`: **(Dockerで必須)** データベースを永続ボリュームに保存するため、`/app/data`に設定してください。
- `NOTIFICATION_SUPPRESSION_SECONDS`: (任意) 同じ種類の取引に対する通知間のクールダウン時間（秒）。デフォルトは`60`です。

**`.env`の例:**
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your_webhook_id/your_webhook_token
NOTIFICATION_SUPPRESSION_SECONDS=300
DB_DIRECTORY=/app/data
```

### 3. サービスの実行

設定が完了したら、以下のコマンドでサービスを管理します。

- **バックグラウンドでサービスを開始:**
  ```bash
  docker-compose up -d
  ```

- **リアルタイムでログを表示:**
  ```bash
  docker-compose logs -f
  ```
  *(`Ctrl+C`でログ表示を終了しても、サービスは実行され続けます。)*

- **サービスを停止:**
  ```bash
  docker-compose down
  ```

---

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
