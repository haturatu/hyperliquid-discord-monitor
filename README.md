# hyperliquid-discord-monitor

Monitor Hyperliquid user fills over WebSocket and post position activity to Discord. This repository incorporates application improvements from [deg-labs/hdm](https://github.com/deg-labs/hdm), without its GCE-specific deployment setup.

## Features

- Monitor multiple wallets over one WebSocket connection with automatic reconnection.
- Give each wallet an optional label and additional Discord webhook.
- Deduplicate fills using SQLite; seed historical fills on first startup without re-posting.
- Initial Discord POST, PATCH of ongoing fills and a final completion POST after inactivity.
- Persist unfinished notification sessions and retry incomplete completion PATCHes across restarts.
- Preserve the original message link when possible, the size-weighted average fill price, and Long (green)/Short (blue) card colors.
- Discord retry handling, rate-limit awareness, message retention, safe deletion and recovery journals.
- Hyperliquid Ghost links, health heartbeat independent of fills, structured logging and test preview.

## Install

Python 3.10+ is required for local execution; Docker uses Python 3.15.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set `DISCORD_WEBHOOK_URL` in `.env`, then create `addresses.txt` containing one entry per line:

```text
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12,my-label
0x9999999999999999999999999999999999999999,team-alpha,https://discord.com/api/webhooks/id/token
```

The format is `wallet[,tag[,additional_webhook_url]]`. The per-wallet webhook receives **additional** notifications; the default webhook still receives them. Keep `.env` and `addresses.txt` private.

## Run and preview

The original entrypoint filename is preserved; `hdm.py` is also available:

```sh
python hyperliquid-discord-monitor.py monitor addresses.txt
python hyperliquid-discord-monitor.py addresses.txt
python hyperliquid-discord-monitor.py monitor addresses.txt --daemon
python hyperliquid-discord-monitor.py monitor addresses.txt --webhook-url "https://discord.com/api/webhooks/id/token"
```

The `monitor` command can be omitted for backward compatibility. In daemon mode, the PID file is `/tmp/hyperliquid_monitor_multi.pid` and log files are under `/tmp/hyperliquid_monitor_multi*.log`.

Preview incoming fills without a full monitor and without sending Discord messages:

```sh
python hyperliquid-discord-monitor.py tests --addresses-file addresses.txt --timeout-seconds 60 --max-entries 10
```

The preview `--post` option sends to Discord; use it deliberately.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | Required for monitor | Default webhook |
| `NOTIFICATION_SUPPRESSION_SECONDS` | `600` | Quiet seconds since the most recent fill before the final POST |
| `USER_FILL_INACTIVITY_RECONNECT_SECONDS` | `900` | Reconnect after this many seconds without monitored user fills |
| `WEBSOCKET_ACTIVITY_TIMEOUT` | `900` | Legacy fallback for the inactivity setting |
| `DB_DIRECTORY` | `.` | SQLite database and recovery-journal directory |
| `HEALTHCHECK_FILE` | `/tmp/healthcheck.txt` | Monitor-loop heartbeat |
| `DISCORD_KEEP_MESSAGES` | `50` | Per-database retention count |
| `DISCORD_CLEANUP_INTERVAL_HOURS` | `24` | Cleanup interval |
| `DISCORD_MESSAGE_MAX_AGE_HOURS` | `0` | Optional age-based cleanup; 0 disables |
| `DISCORD_DELETE_INTERVAL_SECONDS` | `0.5` | Gap between delete requests |
| `TEST_PREVIEW_TIMEOUT_SECONDS` | `60` | Test-preview wait |
| `TEST_PREVIEW_MAX_ENTRIES` | `10` | Test-preview limit |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO to stdout; WARNING+ to stderr |

`USER_FILL_INACTIVITY_RECONNECT_SECONDS` takes precedence over the legacy timeout variable. Each log entry includes the container hostname and process ID.

## Notification behavior and persistence

For each wallet/market/webhook notification session, the first new fill creates a message. Further fills PATCH the same message, with high-frequency events merged into roughly two-second update sweeps. When no new fill occurs for `NOTIFICATION_SUPPRESSION_SECONDS`, a new position-change-completed message is posted, then the original message is PATCHed to its completed state. Even a single fill eventually produces a completion message. This is a **quiet-period setting**, not a rate limit for initial notifications.

Session direction controls color throughout: Long is green, Short is blue. The completed original post displays the size-weighted average execution price across its fills (not necessarily the position entry price). The final post links to the original post if Discord provides the channel and message IDs. Older sessions attempt to recover the channel ID where possible.

SQLite files `trades_<wallet suffix>.db` store deduplication IDs, session/message IDs, pending completion updates and Discord message retention state. Existing fills are seeded silently at first startup. Discord 429 responses are retried according to the server's retry delay, and transient failures are retried with delays; exactly-once delivery cannot be guaranteed if a Discord POST response is lost.

**Persist the entire DB directory**, including WAL files and `*.discord-pending` recovery journals. A lost DB can cause duplicate notifications or lose unfinished sessions. Active messages are protected from retention cleanup. The optional age limit can remove messages even if the count limit has not been reached.

## Docker Compose

```sh
cp .env.example .env
# Set DISCORD_WEBHOOK_URL and create addresses.txt
docker compose up -d --build
docker compose logs -f
```

The Compose service remains `hyperliquid-monitor`. Its `addresses.txt` mount is read-only, `.env` is loaded **at runtime**, and the image never includes secrets or wallet lists. `/app/data` persists in a named `monitor-data` volume across container recreation. The image runs without root privileges (UID/GID 65534). The healthcheck considers the monitor unhealthy if the heartbeat has not updated for 120 seconds.

If you already use a host `./data` directory, you may replace the named volume with `./data:/app/data`, but ensure UID 65534 can write to it and **preserve existing databases, WAL files and recovery journals**. Running `docker compose down -v` will delete the named volume; do not use it unless you intend to discard monitoring state.

Stop with `docker compose down`.

## Tests

```sh
python -m pip install pytest -r requirements.txt
python -m pytest -q
```

Pull requests build the Docker image and run the application tests. No GCE deployment configuration is included.

## Published Docker image

To run the prebuilt image from GitHub Container Registry, replace `build: .`
in `docker-compose.yml` with:

```yaml
image: ghcr.io/haturatu/hyperliquid-discord-monitor:latest
```

Keep the same `env_file`, `environment`, and read-only `addresses.txt` mount.
Set `DISCORD_WEBHOOK_URL` in your local `.env` file; Compose checks that
it is not empty. Webhook credentials and wallet lists are loaded at runtime,
not included in the published image. Published releases also use the `v*` tag.
