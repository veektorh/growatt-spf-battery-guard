#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="${GROWATT_GUARD_RUNTIME_ROOT:-${ROOT}}"
DATA_ROOT="${GROWATT_GUARD_DATA_DIR:-${ROOT}}"
GUARD_SCRIPT=""
if [[ -z "${GUARD_BIN:-}" ]]; then
  if [[ -x "${RUNTIME_ROOT}/growatt-guard" ]]; then
    GUARD_BIN="${RUNTIME_ROOT}/growatt-guard"
  elif [[ -x "${RUNTIME_ROOT}/.venv/bin/growatt-guard" ]]; then
    GUARD_BIN="${RUNTIME_ROOT}/.venv/bin/growatt-guard"
  else
    GUARD_BIN="${RUNTIME_ROOT}/.venv/bin/python"
    GUARD_SCRIPT="${RUNTIME_ROOT}/growatt_power_guard.py"
  fi
fi
SERVICE_USER="${SUDO_USER:-$(id -un)}"
INTERVAL_MINUTES="${DASHBOARD_REFRESH_MINUTES:-15}"
STALE_CHECK_MINUTES="${DASHBOARD_STALE_CHECK_MINUTES:-10}"
HOST="${DASHBOARD_HOST:-127.0.0.1}"
PORT="${DASHBOARD_PORT:-8080}"

if [[ ! -x "${GUARD_BIN}" || ( -n "${GUARD_SCRIPT}" && ! -f "${GUARD_SCRIPT}" ) ]]; then
  echo "Packaged Growatt Guard executable not found at ${GUARD_BIN}"
  echo "Run ./update_server.sh to create and activate a release."
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
WorkingDirectory=${RUNTIME_ROOT}
Environment="GROWATT_GUARD_HOME=${RUNTIME_ROOT}"
Environment="GROWATT_GUARD_DATA_DIR=${DATA_ROOT}"
ExecStart=${GUARD_BIN}${GUARD_SCRIPT:+ ${GUARD_SCRIPT}} observability-refresh --loop --interval-minutes ${INTERVAL_MINUTES}
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
WorkingDirectory=${RUNTIME_ROOT}
Environment="GROWATT_GUARD_HOME=${RUNTIME_ROOT}"
Environment="GROWATT_GUARD_DATA_DIR=${DATA_ROOT}"
ExecStart=${GUARD_BIN}${GUARD_SCRIPT:+ ${GUARD_SCRIPT}} serve-dashboard --host ${HOST} --port ${PORT}
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
WorkingDirectory=${RUNTIME_ROOT}
Environment="GROWATT_GUARD_HOME=${RUNTIME_ROOT}"
Environment="GROWATT_GUARD_DATA_DIR=${DATA_ROOT}"
ExecStart=${GUARD_BIN}${GUARD_SCRIPT:+ ${GUARD_SCRIPT}} dashboard-stale-alert --output ${DATA_ROOT}/dashboard.html
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
