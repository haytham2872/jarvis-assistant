# CLAUDE.md

Guidance for Claude Code and other coding agents working in this repository. It is not
part of JARVIS's own self-knowledge: what JARVIS reads about himself is
[docs/SELF_MODEL.md](docs/SELF_MODEL.md).

## What this is

JARVIS is a personal AI assistant that lives on a server and works for one person, its
owner. It runs two ways from the same Python backend (FastAPI + asyncio):

- **Linux server (primary, always on):** reached through a private Discord DM and an
  optional iPhone app. No microphone, speech or desktop UI. Reminders, scheduled tasks,
  research, watches and the morning brief run here as background loops.
- **Windows desk (optional):** adds a wake word, speech in and out, and an Electron +
  React UI with a Three.js "neural sphere". Started with `scripts/run.ps1`.

JARVIS has no shell, terminal, Git, screen or computer-control tool on either host.

### The owner

Who JARVIS works for comes from two settings, read by [backend/owner.py](backend/owner.py):
`JARVIS_OWNER_NAME` (empty: "the user") and `JARVIS_OWNER_PRONOUNS` (he, she or they).
Model-facing text never names a person. It writes the owner as markers, `«owner»`,
`«Owner»`, `«owner's»`, `«OWNER»`, `«he»`, `«He»`, `«him»`, `«his»`, `«His»`, `«himself»`,
`«theirs»`, and wraps the string in `_owner.say(...)`, which fills them in when the module
loads (`from backend import owner as _owner`). With "they", the verb right after «he» is
put in the plural, so write «he» before a present-tense verb or a modal, never "«he»'s".
`backend/test_owner.py` fails on a marker that is not inside `say()`, and renders the
prompt and tool schemas for another owner to check that no marker or name is left.
"He" for JARVIS himself stays a plain word.

## Architecture

```
Discord text/voice ─┐
iPhone app ─────────┤
Desk text/audio ────┴→ main.py → brain/engine.py → selected model provider
                                  ↑       ↕
                           context/service.py   tools/gateway.py → registered tools
                                  ↑
                         SQLite exchanges + Qdrant index + vault
```

- `backend/brain/` — the conversation engine (`engine.py`), personality and prompt
  (`personality.py`), memory, scheduling (`chronos.py`), research, goals, portfolio, skills.
- `backend/tools/` — the registered tools (`registry.py`), the shared tool gateway and the
  action policy that decides what needs the owner's approval.
- `backend/storage/` — SQLite (exchanges, runs, operation receipts, jobs, approvals, goals).
- `backend/context/` — the context service: which sources reach a turn, within one budget.
- `backend/providers/` — model client lifetime (OpenRouter, the only provider).
- `backend/mobile/` — the iPhone app's API. `mobile/` is the Expo app itself.
- `frontend/` — the Electron desk UI.
- `jarvis-vault/` — JARVIS's Obsidian-compatible notes (created on first run, never committed).

The tool registry is locked by `backend/parity/tool_registry.json`; regenerate it with
`python -m backend.parity.snapshot` when the tool surface changes on purpose.

## How to work

- **Think before coding.** State assumptions; when a request has two readings, say so.
- **Simplicity first.** The least code that solves the problem.
- **Surgical changes.** Every changed line traces to the request; match the surrounding style.
- **Goal-driven.** Turn the task into a check (a failing test that reproduces the bug, then
  passes) and loop until it holds.

**Background work goes through `backend.tasks.spawn(coro)`**, never a bare
`asyncio.create_task(...)` whose result is dropped: the loop holds tasks weakly.
`test_no_task_is_started_and_dropped` fails on a new one.

**`chat()` in `engine.py` is large.** Grep for a name before introducing it as a new local:
Python makes a name local to the whole function once it is assigned anywhere in it, and a
module import of the same name then breaks earlier in the function.
`backend/brain/test_engine_chat_wiring.py` guards this statically.

**A code map** lives at `graphify-out/graph.json`: every function, class and file as a
node, every import and call as an edge, with `file:line`.

```bash
python -m graphify explain "record_thread"      # a symbol and everything wired to it
python -m graphify affected "vault_write"       # what a change would break
python -m graphify update .                     # rebuild after changing code (~10s, no LLM)
```

**A new or changed capability is not done until JARVIS can see it.** He only knows what his
prompt and tool schemas tell him. In the same change as the code:

1. **Tool registry** (`backend/tools/registry.py`): the tool's description and every
   parameter's description, plus any neighbouring tool that should point at it. A new tool
   also needs its `TOOL_DISPATCH` entry in `engine.py`.
2. **System prompt** (`_INVARIANT_CORE` in `backend/brain/personality.py`): what it does and
   when to use it. A drift test fails if a registered tool is not named there.
3. **Self-model** (`docs/SELF_MODEL.md`, especially "Active tools and boundaries" with the
   tool count). Each digest section is capped (`self_model._SECTION_CHAR_CAP`); both the cap
   and the phrase "no shell, terminal, Git" are tested.
4. **Parity fixture**: `python -m backend.parity.snapshot`, and the tool-count test in
   `backend/parity/test_parity_contract.py`.
5. **Budget**: if the schemas outgrow `Reservation.tools` in `backend/context/budget.py`,
   raise it and `DEFAULT_CONTEXT_WINDOW` by the same amount.
6. **Background allowlist** (`background._ALLOWED_TOOLS`): decide whether background
   workers get it.
7. **Owner-facing text**: the `/about` pitch (`backend/brain/pitch.py`), `/commands`,
   `.env.example` and `DEPLOY.md`.
8. **Check live** that the model actually picks it with the real prompt and tools.

## Commands

```powershell
.\scripts\setup.ps1                 # Windows desk, first time: .venv, npm, models, Docker
.\scripts\run.ps1                   # full desk system (Vite dev server)
.\scripts\run.ps1 -BackendOnly      # backend only
.\scripts\run.ps1 -NoUI             # terminal client, no Electron, no speech
.\scripts\run.ps1 -Model openrouter # OpenRouter's DeepSeek models
```

The Linux server is described in [DEPLOY.md](DEPLOY.md); flags in [RUNNING.md](RUNNING.md).

### Tests

```bash
pytest                                      # the whole suite, in parallel on every core
pytest --testmon                            # only the tests your change affects
pytest backend/brain/test_research.py       # one file, in one process
```

`pytest.ini` runs in parallel (pytest-xdist, `-n auto`); `backend/conftest.py` keeps a single
file or test, and `--testmon`, in one process. pytest-testmon records which code each test
runs (`.testmondata`, gitignored) and reruns only the tests whose code changed. It cannot see
files a test reads (`docs/SELF_MODEL.md`, `.env.example`, `tool_registry.json`), so it is
for working; the full suite runs before a commit.

Tests live next to their modules. No test reaches a real model, Qdrant, the vault or the
SQLite database: `backend/conftest.py` redirects every store and sets the test owner
("Sam", he). Pure-function-first style: monkeypatch module-level paths to `tmp_path`, stub
model calls, and assert delivered-exactly-once for anything routed through a `pop_*` function.
