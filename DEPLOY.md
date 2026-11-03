# Running JARVIS on a server

[docs/SETUP.md](docs/SETUP.md) installs JARVIS and starts it as a service. This page is about
running it afterwards: choosing a machine, updating, backups and restoring, the time zone, the
phone app, and goals.

## The machine

JARVIS is one Python process (FastAPI) plus two containers: Qdrant (memory) and SearXNG (web
search). It needs no GPU.

- **2 vCPU, 4 GB RAM, 20 GB disk** is comfortable. With 2 GB of RAM, add swap:
  ```bash
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  ```
- The voice-note model uses about 700 MB while loaded and is released after 10 idle minutes;
  text startup loads none of the audio stack.
- **Open no ports.** The backend binds to 127.0.0.1, Qdrant and SearXNG to the loopback
  address too, and Discord is an outgoing connection. SSH is the only thing a firewall needs to
  allow. The backend's WebSocket has no login, so never set `JARVIS_HOST` to a public address.

## Updating

```bash
cd ~/jarvis            # wherever you cloned it
git pull
.venv/bin/pip install -r requirements.txt     # when requirements.txt changed
sudo systemctl restart jarvis
journalctl -u jarvis -f                        # confirm it came back up
```

A restart is safe at any time: the running conversation is saved first, an answer cut off by
the restart is reported to you on Discord, and the conversation picks up where it was.

The Qdrant and SearXNG containers are pinned to tested versions in
`mem0/docker-compose.yml`, and `qdrant-client` in `requirements.txt` matches the Qdrant one.
When an update changes those pins, take a backup first (below): a new Qdrant version can
migrate its storage, and an older one cannot read it back. Then
`docker compose -f mem0/docker-compose.yml up -d` before restarting JARVIS.

## Backups

Everything JARVIS knows lives in three places: the vault (`jarvis-vault/`, Markdown), the
SQLite database (`.jarvis_runtime.db`: conversations, runs, the portfolio, reminders, plans)
and the Qdrant volume (the search index and extracted facts).

JARVIS backs all three up once a day into `backups/<date>/` and keeps the last seven verified
copies. To run one now:

```bash
.venv/bin/python -m backend.tools.backup
```

Copy that folder off the machine as well, for example with a daily cron line using `rclone`
or `rsync`. **Never commit `jarvis-vault/` or the database to git**: they hold your
conversations, and both are ignored for that reason.

### Restoring the database

Stop JARVIS, copy the backup's `runtime.db` over `.jarvis_runtime.db` (delete any
`.jarvis_runtime.db-wal` and `-shm` beside it first), mark the restore and start JARVIS:

```bash
sudo systemctl stop jarvis
cp backups/<date>/runtime.db .jarvis_runtime.db
touch .jarvis_runtime.db.restored
sudo systemctl start jarvis
```

An old copy may not know about emails or research that already happened. With the marker
present, every goal starts paused and its pending approval requests are withdrawn; `/goals
<id>` lists any action whose outcome is not confirmed. Resume each goal only after looking.
The marker is removed once read.

The vault restores by unzipping `vault.zip` into `jarvis-vault/`; the Qdrant collections from
the `.snapshot` files through Qdrant's
[snapshot API](https://qdrant.tech/documentation/concepts/snapshots/), or by letting JARVIS
re-index the vault (it re-indexes any note whose content changed, every 30 minutes).

## Time zone

Set `JARVIS_TIMEZONE` in `.env` (an IANA name such as `America/New_York`). JARVIS runs its own
process on that zone whatever the host says, so reminders, the morning brief and the calendar
are right. Setting the host to the same zone (`sudo timedatectl set-timezone ...`) makes cron
and `journalctl` times match too.

## Costs

The model bill is OpenRouter's: DeepSeek V4.1 Flash, about $0.30 per million input tokens and
$1.20 per million output tokens at peak (half that off-peak), with most of each turn's input
read from the prompt cache at a fiftieth of the price. Normal daily use, including the
background research and the morning brief, costs cents a day. `/cost` shows the session's
spending by model, and `/pause` stops the background work.

A small cloud VM costs from about €5 a month at budget providers to $25-35 at the large clouds.

## The phone app

The iOS app in `mobile/` reaches the server over [Tailscale](https://tailscale.com/), never the
open internet: `tailscale serve` publishes only the `/mobile` routes, over HTTPS, to your own
devices.

1. Install Tailscale on the server
   (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`) and on the phone,
   signed in to the same account.
2. In the Tailscale admin console's DNS page, turn on MagicDNS and HTTPS Certificates.
3. Make a token and put it in `.env` as `MOBILE_TOKEN`:
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
4. Publish the routes:
   `sudo tailscale serve --bg --https=443 --set-path /mobile http://127.0.0.1:8765/mobile`
   (`tailscale serve status` shows it; `--set-path /mobile off` removes it).
5. Restart JARVIS. On a computer with Node:
   `MOBILE_TOKEN=<the same token> node mobile/tools/pair_qr.mjs --base https://<server>.<tailnet>.ts.net --svg pair.svg`,
   then scan it from the app's Server screen.
6. Optional: spoken answers in Google's Chirp 3 HD voice. Enable the Cloud Text-to-Speech API,
   create an API key restricted to it, and set `GOOGLE_TTS_API_KEY`. The free tier is a million
   characters a month; JARVIS stops at `MOBILE_TTS_MONTHLY_CHARS` (900,000) and the phone then
   reads with its own voice.

Without `MOBILE_TOKEN` the `/mobile` routes answer 404. To revoke the phone, change or remove the
token.

The app itself is an Expo project: `cd mobile && npm ci`, then run it in Expo Go or build it
with EAS under your own Expo account.

## Goals

Goals stay off until `JARVIS_GOALS=1` is set. `GOAL_DEFAULT_BUDGET_USD` (1.0) and
`GOALS_DAILY_BUDGET_USD` (3.0) cap what they spend. Only one JARVIS process runs goals: it holds
`.jarvis_runtime.db.goals.lock`, and a second process on the same database logs that goals are
off for it. Turning goals off stops new sessions and keeps every goal's records.

## Google sign-in expiring

If Gmail or the calendar stop working with `invalid_grant`, the Google OAuth app is still in
Testing mode, where Google expires the sign-in after seven days. Publish the app in the Google
Cloud console, sign in again on a machine with a browser, and copy `token.json` and
`token_calendar.json` across (docs/SETUP.md, Gmail and Google Calendar).
