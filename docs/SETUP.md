# Setting up JARVIS

JARVIS runs as one Python process next to two containers (Qdrant for memory, SearXNG for web
search). You talk to it through a Discord bot in a private DM. The minimum is an OpenRouter key
and a Discord bot; everything else (web research, email, calendar, the phone app) is optional
and can be added later.

This guide sets it up on a Linux machine, the always-on setup it is built for. On Windows, see
[Windows desk](#windows-desk) at the end.

## What you need

- **A Linux machine that stays on**: a small cloud VM is enough (2 vCPU, 4 GB RAM, 20 GB disk;
  with 2 GB of RAM add swap). Ubuntu 24.04 and Debian 12 work.
- **Python 3.10 to 3.12** with its venv module (`sudo apt install python3-venv`), and `git`.
- **Docker with the Compose plugin** ([install guide](https://docs.docker.com/engine/install/)).
- **An OpenRouter account** with a few dollars of credit. Normal use costs cents a day: chat,
  background work and memory all run on DeepSeek V4.1 Flash.
- **A Discord account**.

## 1. Install

```bash
git clone https://github.com/haytham2872/jarvis-assistant.git jarvis
cd jarvis
bash scripts/setup.sh
```

`setup.sh` creates `.env` from `.env.example`, makes a virtual environment, installs the
packages (CPU-only PyTorch, so no multi-gigabyte GPU build), downloads the local models
(embedder, reranker, BM25, spaCy: about 400 MB), writes a SearXNG configuration with its own
secret and starts the two containers. It takes 5 to 15 minutes and is safe to re-run.

Add `--voice` to also download the voice-note model (Parakeet, 640 MB). Without it, voice notes
fall back to Whisper.

## 2. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and click
   **New Application**. The name is what you will see in Discord.
2. On the **Bot** tab: click **Reset Token**, copy the token, and turn on
   **Message Content Intent** (without it every message arrives empty).
3. On the **OAuth2** tab, under **OAuth2 URL Generator**: tick `bot`, then under bot permissions
   tick **Send Messages**, **Read Message History** and **Attach Files**. Open the generated URL
   and add the bot to a server you own (create a private one with just you if needed). A bot
   can only DM people it shares a server with.

## 3. Fill in `.env`

Open `.env`. Only the first section is required:

```ini
OPENROUTER_API_KEY=sk-or-...          # https://openrouter.ai/keys
DISCORD_BOT_TOKEN=...                 # from step 2
DISCORD_ALLOWED_USER_ID=              # leave empty: step 4 fills it in
```

The next section makes JARVIS yours. Optional, but set them:

```ini
JARVIS_OWNER_NAME=Alex                # how JARVIS refers to you (empty: "the user")
JARVIS_OWNER_PRONOUNS=they            # he, she or they
JARVIS_TIMEZONE=Europe/Berlin         # IANA name; reminders, briefs and the calendar use it
JARVIS_BASE_CURRENCY=EUR              # the portfolio totals in this currency
```

Everything below that is commented out. Leave a line commented rather than empty: an empty value
is not the same as unset (`OPENROUTER_STANDARD_MODEL=` would select no model at all).

## 4. First run

```bash
.venv/bin/python -m backend.main --no-wake
```

Watch it start (`JARVIS ready` and `Discord bridge active as <bot name>`). With no owner set,
the log shows a one-time code:

```text
Discord bridge: no owner yet (DISCORD_ALLOWED_USER_ID is empty). To make yourself the owner,
send the bot this DM: /claim K7QW3MXP  (works once; a restart makes a new code)
```

Send your bot that DM. JARVIS answers that you are the owner and writes your Discord id into
`.env` as `DISCORD_ALLOWED_USER_ID`, so it holds after a restart. Until then it answers nobody,
and from then on only you. The code works only in a DM, only once, and five wrong tries switch
it off until the next restart. (You can also set `DISCORD_ALLOWED_USER_ID` yourself: every
ignored sender's id is in the log.) Now try:

- `/status`: uptime, the model, background work, memory health;
- `/commands`: everything you can type;
- an ordinary message, a reminder ("remind me in 10 minutes to stretch"), a question that needs
  the web.

If it fails to start, the log says why: a missing `OPENROUTER_API_KEY` stops it before it
accepts anything, and a model that was not downloaded is named.

## 5. Keep it running

```bash
sudo bash scripts/install_service.sh
journalctl -u jarvis -f          # follow the log; Ctrl+C stops watching, not JARVIS
```

This installs `deploy/jarvis.service`: it starts JARVIS at boot, restarts it if it crashes, and
gives it 90 seconds on shutdown to save the open conversation. Run it as the same user who ran
`setup.sh`, because the models are cached in that user's home.

[DEPLOY.md](../DEPLOY.md) covers running it day to day: updating, backups and restoring, the
time zone, and memory use.

## Optional integrations

Each one is off until its key is set in `.env`; restart JARVIS after changing `.env`.

### Web search, page reading and research reports: TinyFish

Free, limited by rate rather than by a monthly quota. Get a key at
[agent.tinyfish.ai](https://agent.tinyfish.ai/api-keys) and set `TINYFISH_API_KEY`. Without it,
search goes through the local SearXNG container and then keyless scrapers, which search engines
often block from cloud servers.

### Gmail and Google Calendar

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project, enable the
   **Gmail API** and **Google Calendar API**, and configure the OAuth consent screen (External,
   add yourself as a test user). Then **publish** the app: in Testing mode Google expires the
   sign-in after seven days.
2. Create an OAuth client ID of type **Desktop app** and put its ID and secret in `.env` as
   `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
3. Sign in once on a machine with a browser (a server has none). On your own computer, with the
   project cloned, the packages installed and the same two settings in its `.env`:

   ```bash
   .venv/bin/python -c "from backend.tools import gmail, calendar; gmail._get_service(); calendar._get_service()"
   ```

   Two consent pages open. Then copy `token.json` and `token_calendar.json` to the project folder
   on the server.

JARVIS reads mail and your calendar on its own, but only sends an email when you asked for it
in that message. Anything else becomes an approval request you tap.

### Notion

Create an internal integration at [notion.so/my-integrations](https://www.notion.so/my-integrations),
then two databases and share both with the integration (database menu, Connections):

- **Ideas**: Title, Status (select: Raw, Organized), Tags (multi-select), Created (created time);
- **Tasks**: Title, Status (the *status* type), Due (date), Priority (select: Low, Medium, High).

Set `NOTION_API_KEY`, `NOTION_IDEAS_DB_ID` and `NOTION_TASKS_DB_ID` (the ID is in each database's
URL).

### Spotify

Playback control needs Spotify Premium. Create an app in the
[Spotify dashboard](https://developer.spotify.com/dashboard) with the redirect URI
`http://127.0.0.1:8888/callback`, and set `SPOTIFY_CLIENT_ID` (no secret: it signs in with PKCE).
Like Google, the first sign-in opens a browser; do it on your own computer and copy
`spotify_token.json` across.

### The phone app

The iOS app in `mobile/` (Expo) reaches the server over [Tailscale](https://tailscale.com/), so
nothing is exposed to the internet. [DEPLOY.md](../DEPLOY.md#the-phone-app) has the steps.

### Goals

Work that runs past one answer: milestones, saved evidence, a budget, its own background
sessions. Off by default; set `JARVIS_GOALS=1`. `GOAL_DEFAULT_BUDGET_USD` (1.0) and
`GOALS_DAILY_BUDGET_USD` (3.0) cap the spending.

## Windows desk

The same backend runs on Windows with a microphone, a wake word ("hey Jarvis"), spoken answers
and a desktop window (Electron). It needs Python 3.10-3.12, Node 18+ and Docker Desktop.

```powershell
.\scripts\setup.ps1      # first time: packages, models, containers
.\scripts\build.ps1      # once: the desktop window's production build
.\scripts\use.ps1        # daily use
```

`.\scripts\run.ps1 -NoUI` gives a terminal chat instead of the window, and `-NoWake` drops the
microphone. [RUNNING.md](../RUNNING.md) lists every flag.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `OPENROUTER_API_KEY is not set` at startup | The key is missing or the line is commented in `.env`. |
| The bot is online but never answers | Message Content Intent is off, or `DISCORD_ALLOWED_USER_ID` is not your id (check the log). |
| `/claim` gets no answer | It was not a DM, the code is from an earlier start, five wrong tries switched it off (restart for a new code), or Message Content Intent is off. |
| `LocalEntryNotFoundError` or `OfflineModeIsEnabled` | A model was not downloaded: run `.venv/bin/python scripts/fetch_models.py` as the user JARVIS runs as. |
| `/status` says memory is degraded | Qdrant is not running: `docker compose -f mem0/docker-compose.yml up -d`. |
| SearXNG keeps restarting | `searxng/settings.yml` is missing or became a directory: remove it and run `.venv/bin/python scripts/searxng_settings.py`. |
| Web search finds nothing | Set `TINYFISH_API_KEY`; cloud addresses are often blocked by the keyless engines. |
