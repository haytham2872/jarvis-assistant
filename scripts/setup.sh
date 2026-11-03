#!/usr/bin/env bash
# JARVIS setup for Linux (a server or a desktop). Safe to re-run.
#
#   bash scripts/setup.sh               # everything: .env, venv, packages, models, containers
#   bash scripts/setup.sh --voice       # also download the voice-note model (~640 MB)
#   bash scripts/setup.sh --no-docker   # skip starting Qdrant and SearXNG
#
# Needs Python 3.10-3.12 and Docker with the Compose plugin. docs/SETUP.md walks through it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VOICE=0
DOCKER=1
for arg in "$@"; do
    case "$arg" in
        --voice) VOICE=1 ;;
        --no-docker) DOCKER=0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }

# --- Prerequisites ------------------------------------------------------------------------
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python 3 not found. Install Python 3.10-3.12 (and its venv module), then re-run." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)'; then
    echo "JARVIS needs Python 3.10-3.12; $("$PYTHON" --version) found." >&2
    echo "Point PYTHON at another interpreter, e.g. PYTHON=python3.12 bash scripts/setup.sh" >&2
    exit 1
fi
echo "Python: $("$PYTHON" --version)"

if [ "$DOCKER" = 1 ]; then
    if ! docker compose version >/dev/null 2>&1; then
        echo "Docker with the Compose plugin is required (https://docs.docker.com/engine/install/)." >&2
        echo "Or re-run with --no-docker and point QDRANT_HOST / SEARXNG_URL at your own." >&2
        exit 1
    fi
    echo "Docker: $(docker compose version --short 2>/dev/null || echo ok)"
fi

# --- .env --------------------------------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo ".env created from .env.example -- fill in OPENROUTER_API_KEY (docs/SETUP.md)."
else
    echo ".env already exists, left as it is."
fi

# --- Python environment ------------------------------------------------------------------
say "Python packages (several minutes the first time)"
if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip -q
if ! .venv/bin/python -c 'import torch' >/dev/null 2>&1; then
    # CPU-only torch first: otherwise pip pulls the multi-GB CUDA build, which a server
    # without a GPU never uses (sentence-transformers and kokoro depend on torch).
    .venv/bin/pip install -q torch --index-url https://download.pytorch.org/whl/cpu
fi
.venv/bin/pip install -q -r requirements.txt
echo "Python packages installed."

# --- Models ------------------------------------------------------------------------------
say "Local models (downloaded once)"
if [ "$VOICE" = 1 ]; then
    .venv/bin/python scripts/fetch_models.py --voice
else
    .venv/bin/python scripts/fetch_models.py
fi

# --- Containers --------------------------------------------------------------------------
say "Qdrant (memory) and SearXNG (web search)"
.venv/bin/python scripts/searxng_settings.py
if [ "$DOCKER" = 1 ]; then
    docker compose -f mem0/docker-compose.yml up -d
    for _ in $(seq 1 40); do
        if curl -fsS http://127.0.0.1:6333/readyz >/dev/null 2>&1; then
            echo "Qdrant is ready."
            break
        fi
        sleep 1
    done
    curl -fsS http://127.0.0.1:6333/readyz >/dev/null 2>&1 || echo "Qdrant is not answering yet; JARVIS waits for it at startup."
else
    echo "Skipped (--no-docker)."
fi

# --- Done --------------------------------------------------------------------------------
say "Done"
cat <<EOF
Next:
  1. Put your OpenRouter key (and your Discord bot token) in .env -- docs/SETUP.md.
  2. Start JARVIS and watch it boot:
       .venv/bin/python -m backend.main --no-wake
  3. Keep it running as a service:
       sudo bash scripts/install_service.sh
EOF
