# Running JARVIS — quick reference

## Daily use

The normal way to run JARVIS: hidden windows, output to log files, no mic (Discord is the
interface):

```powershell
.\scripts\use.ps1 -NoWake
```

`-NoWake` disables the wake-word listener (no "hey jarvis", no mic always hot) — the right
choice when Discord is your interface and you don't talk to the mic. `use.ps1` always runs in
`-Quick` mode: backend and frontend both start hidden, with output going to `logs\backend.log`
and `logs\frontend.log` instead of popping up windows. The Discord bridge starts as part of the
backend regardless of whether the frontend UI is ever opened — you don't need Alt+J for Discord
to work, it's there only if you ever want to glance at the desk UI.

Ctrl+C in **this** terminal is still what stops everything (memory flush, sleep marker, then
Qdrant is suspended — never destroyed). Needs `frontend\dist` to exist first — run
`.\scripts\build.ps1` once, and again after any frontend code change.

If you don't need the frontend running at all (pure Discord/backend use, no desk UI), add
`-BackendOnly` via `run.ps1` directly — see below, since `use.ps1` itself
doesn't expose that flag.

## Dev / troubleshooting

```powershell
.\scripts\run.ps1                        # dev server, hot reload, visible windows
.\scripts\run.ps1 -Prod                  # production build instead of dev server (build.ps1 first)
.\scripts\run.ps1 -Quick                 # hide the backend/frontend windows -- logs\*.log instead
.\scripts\run.ps1 -NoWake                # keyboard/typed input only, no microphone
.\scripts\run.ps1 -NoUI                  # terminal REPL only -- no Electron, no TTS
.\scripts\run.ps1 -BackendOnly           # backend + Docker only, skip the frontend
.\scripts\run.ps1 -FrontendOnly          # frontend only, skip Docker + backend
```
Flags compose freely, e.g. `.\scripts\run.ps1 -Prod -Quick -NoWake` (which is exactly
what `.\scripts\use.ps1 -NoWake` runs under the hood), or
`.\scripts\run.ps1 -Prod -Quick -NoWake -BackendOnly` for hidden, Discord-only, no frontend at all.

## Flag reference

| Flag | Effect |
|---|---|
| `-Quick` | Backend + frontend run hidden (`-WindowStyle Hidden`), output to `logs\backend.log` / `logs\frontend.log` instead of two popup PowerShell windows. Everything else is identical -- same Ctrl+C teardown. This is what `use.ps1` always sets. |
| `-NoWake` | No wake-word listener -- input comes from the keyboard/UI/Discord instead of "hey jarvis". Use when you don't want the mic always hot. |
| `-Prod` | Serve the built `frontend\dist` instead of the Vite dev server. Faster, smoother, no hot reload. Requires `.\scripts\build.ps1` to have been run. |
| `-BackendOnly` | Starts Docker + backend, skips the frontend entirely. Combine with `-Quick -NoWake` for a pure Discord/backend session with nothing frontend-related running at all. |
| `-FrontendOnly` | Starts only the frontend, skips Docker + backend (backend must already be running elsewhere). |
| `-NoUI` | Terminal REPL client (`backend/cli.py`) instead of Electron. No TTS, no log file -- the REPL runs interactively in the current window. Useful for quickly typing test messages, not for a hidden/logged daily session (use `-Quick` for that instead). |

## One-time / setup

```powershell
.\scripts\setup.ps1     # first-time install: deps, model weights, Docker containers
.\scripts\build.ps1     # rebuild frontend\dist -- run after any frontend code change, before -Prod
```

`setup.ps1` automates every step in "Manual first-time setup" below. Use the manual steps only if
`setup.ps1` fails partway and you need to see exactly which step broke.

## Stopping

Ctrl+C in the terminal running `run.ps1` / `use.ps1`. This flushes memory (consolidation, sleep marker) before killing anything, then suspends the Docker containers (`docker compose stop`, never `down` -- your memories aren't touched). Don't close the terminal window or kill the process directly; Ctrl+C is what triggers the graceful shutdown sequence.

---

## Manual fallback (bypassing the scripts)

Everything below runs the same pieces `run.ps1`/`setup.ps1` automate, by hand — useful for
debugging a broken script, or to see exactly what each step does. Prefer the scripts above for
normal use.

### Manual first-time setup

```powershell
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
.venv\Scripts\python.exe -c "import openwakeword; openwakeword.utils.download_models()"
.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
.venv\Scripts\python.exe -c "from kokoro import KPipeline; p = KPipeline(lang_code='a'); [_ for _ in p('Hello.', voice='am_adam')]"
cd frontend
npm install
cd ..
docker compose -f mem0/docker-compose.yml up -d
```
Wait ~10 seconds for Qdrant to start, then verify it's healthy (see Debug commands below).

### Manual three-window run

Open three separate PowerShell windows and run one command in each, instead of `run.ps1`:

**Window 1 — Qdrant (memory)**
```powershell
docker compose -f mem0/docker-compose.yml up -d
```
No-op if already running.

**Window 2 — Backend**
```powershell
.venv\Scripts\python.exe -m backend.main
# or, keyboard-only (no microphone needed):
.venv\Scripts\python.exe -m backend.main --no-wake
```

**Window 3 — Frontend**
```powershell
cd frontend
npm run start
```

**Stopping:** Ctrl+C in the backend and frontend windows, then:
```powershell
docker compose -f mem0/docker-compose.yml stop
```
Memories are safe in the Docker volume — never run `docker compose down -v`.

## Debug commands

```powershell
# Check if Qdrant is healthy
Invoke-WebRequest http://localhost:6333/readyz -UseBasicParsing

# View Qdrant collections (stored memories)
Invoke-WebRequest http://localhost:6333/collections -UseBasicParsing

# Check container status
docker compose -f mem0/docker-compose.yml ps

# View Qdrant logs
docker compose -f mem0/docker-compose.yml logs qdrant --tail 50

# Run backend tests
.venv\Scripts\pytest backend/
```
