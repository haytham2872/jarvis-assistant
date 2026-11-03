# Public release plan

Goal: a public GitHub repo that anyone can install and run as their own assistant, with
the same tools, and that reads well for a job application.

This repository (`jarvis-base`) is that project. It started on 3 November 2026 as a copy
of the private repository's tracked files, with no history, and is developed separately:
the private repository keeps running its owner's own JARVIS, unchanged.

## Decisions

- **Full codebase**: backend with tests, the desktop UI, the phone app.
- **No owner name in the code.** Prompts, tool descriptions and tests never name a person.
  The owner comes from `JARVIS_OWNER_NAME` and `JARVIS_OWNER_PRONOUNS` (backend/owner.py).
- **No history.** The copy left out the vault, secrets, logs, backups, archived roadmaps
  and the measurement records, all of which were only in the private repository.

## Phases

1. **Dead-code cleanup** (done in the private repository before the copy).
2. **No hardcoded owner or locale.**
   - 2a, the owner (done): name and pronouns from settings, markers filled in by
     `_owner.say()`, stored values and file names say "owner" (`people/owner.md`), the
     self-model in `docs/SELF_MODEL.md`, a short agent guide in `CLAUDE.md`, the phone app
     without the first owner's Expo account. Checked: rendered with the first owner's
     settings, every import-time prompt and tool schema was byte-identical to the copy.
   - 2b, the timezone (done): `JARVIS_TIMEZONE` (default UTC) is `localtime.ZONE`
     everywhere; prompts say "local time" or name the zone (`«tz»`); the phone app shows
     the phone's own zone. The tests run in Europe/Paris (root `conftest.py`), which has
     real clock changes.
   - 2c, the currency (done): `JARVIS_BASE_CURRENCY` (default EUR). Rates are stored as
     "X/BASE", crossed through the ECB's euro rates; the ledger's views and totals, the
     morning and close summaries, the charts and the phone app use the base; the `_eur`
     fields are `_base`.
3. **Installable by someone else** (done).
   - 3a, one provider: OpenRouter is the only model provider. `MODEL_MODE`, the Claude and
     Kimi modes and the Anthropic fallbacks are gone, so a new install needs one key.
   - 3b, the install: `docs/SETUP.md` (OpenRouter key and Discord bot, then each optional
     integration), `DEPLOY.md` rewritten for any server (updates, backups and restore, the
     phone over Tailscale), `.env.example` in sections with every optional setting
     commented out, `scripts/setup.sh` (venv, CPU-only torch, models, SearXNG settings with
     a fresh secret, containers), `scripts/fetch_models.py`, `deploy/jarvis.service` with
     `scripts/install_service.sh`. mem0's analytics are off unless `MEM0_TELEMETRY=True`.
   - Fresh install, a clean `python:3.12-slim` container with an empty vault and no `.env`:
     setup took 5 minutes; without a key JARVIS refused to start and named it; with one it
     created the vault, answered two turns (the time right in New York) and stored them,
     about $0.01. It found three bugs, each now fixed and tested:
     - python-dotenv reads `KEY=   # note` as the value `# note`, so a copied
       `.env.example` made the comment text the OpenRouter key (and the owner's name);
     - a typed turn on a machine without audio failed on PortAudio instead of answering
       in text;
     - a self-model section ran 1 character over its cap with no name and "they": the
       length depends on the owner, and the tests only rendered "Sam", "he".
   - Guarded by `backend/test_install_files.py` (every setting the code reads is
     documented and vice versa, a copied example keeps every default, the service and
     setup files) and owner-varied self-model caps.
4. **Secrets gate before the first push** (done).
   - Done: the Qdrant and SearXNG images and `qdrant-client` are pinned to tested versions;
     `/claim <code>` makes the first owner without reading ids out of a log;
     `brain/claude.py` is `brain/engine.py` (the conversation engine), since it no longer has
     anything to do with one provider; `conversation.py` was taken by the phone app's history.
   - Scan of the tree and every commit (key formats for OpenRouter, Anthropic, OpenAI,
     Discord, Google, GitHub, Notion, private keys; the owner's name and email, the old
     SearXNG key, the Spotify client id, Tailscale names, home paths): the tree holds no key
     and no personal detail after removing a surname from a test and "Hetzner" from four
     comments. The remaining addresses are made-up test fixtures (`anna@gmail.com`,
     `lea.moreau@gmail.com`) and `*.ts.net` placeholders.
   - **The history is not publishable**: the first commit (`50f764c`) holds the owner's name
     about 1,700 times, his email, the old SearXNG key and the Spotify client id; a later one
     holds his surname. The first push goes from a new orphan branch holding only the final
     tree; `main` with its history stays local.
   - gitleaks 8.21.2: no leak in the tracked files (its 16 hits are file hashes in the ignored
     `graphify-out/cache/`), one in the history (the old SearXNG key in `50f764c`).
   - The `public` branch is one commit with no parent, holding the same files as `main`.
     gitleaks finds nothing in it, and neither does the personal-terms search. It is what
     gets pushed; `main` and its history stay on this machine.
5. **Showcase.** README (pitch, demo GIF, architecture diagram, measured highlights),
   `docs/engineering-log.md` (about twelve real incidents, no personal details), GitHub
   Actions running `pytest backend/` for a badge, MIT license, push protection on.

## Recommendations (to schedule, not yet decided)

- **`/setup` on Discord for a new owner.** On first launch JARVIS asks the owner's name,
  timezone and currency in the chat and writes them to `.env`, so nobody has to edit a
  config file.
- **`docker compose up` for the whole server.** The compose file covers Qdrant and SearXNG
  today; a backend image would make "install it on a small VPS" one command.
- **A demo mode with a sample vault.** Someone can clone it and see memory, the portfolio
  and goals working with made-up data, without connecting any account.
