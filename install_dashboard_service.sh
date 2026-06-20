#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT}/.venv/bin/python"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
INTERVAL_MINUTES="${DASHBOARD_REFRESH_MINUTES:-10}"
HOST="${DASHBOARD_HOST:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8080}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Virtual environment not found at ${PYTHON_BIN}"
  echo "Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

sudo tee /etc/systemd/system/growatt-dashboard-refresh.service > /dev/null <<EOF
[Unit]
Description=Growatt dashboard refresh
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${ROOT}
ExecStart=${PYTHON_BIN} ${ROOT}/growatt_power_guard.py dashboard-refresh --interval-minutes ${INTERVAL_MINUTES}
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/growatt-dashboard-server.service > /dev/null <<EOF
[Unit]
Description=Growatt dashboard static server
After=network-online.target growatt-dashboard-refresh.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${ROOT}
ExecStart=${PYTHON_BIN} ${ROOT}/growatt_power_guard.py serve-dashboard --host ${HOST} --port ${PORT}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now growatt-dashboard-refresh.service growatt-dashboard-server.service

echo "Installed dashboard services."
echo "Refresh interval: ${INTERVAL_MINUTES} minutes"
echo "Server: http://${HOST}:${PORT}/dashboard.html"
echo "View through SSH tunnel: ssh -L ${PORT}:localhost:${PORT} ${SERVICE_USER}@YOUR_VPS_IP"
