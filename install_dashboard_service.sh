#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT}/.venv/bin/python"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
INTERVAL_MINUTES="${DASHBOARD_REFRESH_MINUTES:-10}"
STALE_CHECK_MINUTES="${DASHBOARD_STALE_CHECK_MINUTES:-10}"
HOST="${DASHBOARD_HOST:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8080}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Virtual environment not found at ${PYTHON_BIN}"
  echo "Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

sudo tee /etc/systemd/system/growatt-dashboard-refresh.service > /dev/null <<EOF
[Unit]
Description=Growatt dashboard and PVOutput refresh
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${ROOT}
ExecStart=${PYTHON_BIN} ${ROOT}/growatt_power_guard.py observability-refresh --loop --interval-minutes ${INTERVAL_MINUTES}
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

sudo tee /etc/systemd/system/growatt-dashboard-stale-alert.service > /dev/null <<EOF
[Unit]
Description=Growatt dashboard stale alert
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${ROOT}
ExecStart=${PYTHON_BIN} ${ROOT}/growatt_power_guard.py dashboard-stale-alert --output ${ROOT}/dashboard.html
EOF

sudo tee /etc/systemd/system/growatt-dashboard-stale-alert.timer > /dev/null <<EOF
[Unit]
Description=Run Growatt dashboard stale alert checks

[Timer]
OnBootSec=5min
OnUnitActiveSec=${STALE_CHECK_MINUTES}min
Persistent=true
Unit=growatt-dashboard-stale-alert.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable growatt-dashboard-refresh.service growatt-dashboard-server.service growatt-dashboard-stale-alert.timer
sudo systemctl restart growatt-dashboard-refresh.service growatt-dashboard-server.service growatt-dashboard-stale-alert.timer

echo "Installed dashboard services."
echo "Refresh interval: ${INTERVAL_MINUTES} minutes"
echo "Stale alert check interval: ${STALE_CHECK_MINUTES} minutes"
echo "Server: http://${HOST}:${PORT}/dashboard.html"
echo "View through SSH tunnel: ssh -L ${PORT}:localhost:${PORT} ${SERVICE_USER}@YOUR_VPS_IP"
