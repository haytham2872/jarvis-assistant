#!/usr/bin/env bash
# Install JARVIS as a systemd service that starts at boot and restarts on failure.
#
#   sudo bash scripts/install_service.sh            # runs as the user who called sudo
#   sudo bash scripts/install_service.sh alice      # or as a named user
#
# Run scripts/setup.sh first, as that same user (the models are cached in their home).
set -euo pipefail

if [ "$(id -u)" != 0 ]; then
    echo "Run with sudo: sudo bash scripts/install_service.sh" >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AS="${1:-${SUDO_USER:-}}"
if [ -z "$RUN_AS" ] || [ "$RUN_AS" = root ]; then
    echo "Name the user JARVIS runs as: sudo bash scripts/install_service.sh <user>" >&2
    exit 1
fi
if ! id "$RUN_AS" >/dev/null 2>&1; then
    echo "No such user: $RUN_AS" >&2
    exit 1
fi
if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "No virtual environment in $ROOT -- run scripts/setup.sh first (as $RUN_AS)." >&2
    exit 1
fi
if [ ! -f "$ROOT/.env" ]; then
    echo "No .env in $ROOT -- run scripts/setup.sh and fill it in first." >&2
    exit 1
fi
sed -e "s|__USER__|$RUN_AS|g" -e "s|__DIR__|$ROOT|g" "$ROOT/deploy/jarvis.service" \
    > /etc/systemd/system/jarvis.service
systemctl daemon-reload
systemctl enable --now jarvis
echo "Installed and started. Follow the log with:  journalctl -u jarvis -f"
