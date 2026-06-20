#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT}/.venv/bin/python"
SERVICE_USER="${SUDO_USER:-$(id -un)}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Virtual environment not found at ${PYTHON_BIN}"
  echo "Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

sudo tee /etc/systemd/system/growatt-discord-control.service > /dev/null <<EOF
[Unit]
Description=Growatt Discord control bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${ROOT}
ExecStart=${PYTHON_BIN} ${ROOT}/growatt_power_guard.py serve-discord-bot
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable growatt-discord-control.service
sudo systemctl restart growatt-discord-control.service

echo "Installed Discord control bot service."
echo "Check status: sudo systemctl status growatt-discord-control.service"
