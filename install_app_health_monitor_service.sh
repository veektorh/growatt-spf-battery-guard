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
CHECK_MINUTES="${APP_HEALTH_CHECK_MINUTES:-5}"

if [[ ! "${CHECK_MINUTES}" =~ ^[1-9][0-9]*$ ]]; then
  echo "APP_HEALTH_CHECK_MINUTES must be a positive integer."
  exit 2
fi
if [[ ! -x "${GUARD_BIN}" || ( -n "${GUARD_SCRIPT}" && ! -f "${GUARD_SCRIPT}" ) ]]; then
  echo "Packaged Growatt Guard executable not found at ${GUARD_BIN}"
  echo "Run ./update_server.sh to create and activate a release."
  exit 1
fi
sudo tee /etc/systemd/system/growatt-app-health-monitor.service > /dev/null <<EOF
[Unit]
Description=Local application health monitor and bounded recovery
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${RUNTIME_ROOT}
Environment="GROWATT_GUARD_HOME=${RUNTIME_ROOT}"
Environment="GROWATT_GUARD_DATA_DIR=${DATA_ROOT}"
ExecStart=${GUARD_BIN}${GUARD_SCRIPT:+ ${GUARD_SCRIPT}} app-health-monitor
TimeoutStartSec=2min
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/growatt-app-health-monitor.timer > /dev/null <<EOF
[Unit]
Description=Check local application health

[Timer]
OnBootSec=3min
OnUnitActiveSec=${CHECK_MINUTES}min
AccuracySec=30s
Persistent=true
Unit=growatt-app-health-monitor.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable growatt-app-health-monitor.timer
sudo systemctl restart growatt-app-health-monitor.timer

echo "Installed app health monitor timer."
echo "Check interval: ${CHECK_MINUTES} minutes"
