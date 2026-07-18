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

if [[ ! -x "${GUARD_BIN}" || ( -n "${GUARD_SCRIPT}" && ! -f "${GUARD_SCRIPT}" ) ]]; then
  echo "Packaged Growatt Guard executable not found at ${GUARD_BIN}"
  echo "Run ./update_server.sh to create and activate a release."
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
WorkingDirectory=${RUNTIME_ROOT}
Environment="GROWATT_GUARD_HOME=${RUNTIME_ROOT}"
Environment="GROWATT_GUARD_DATA_DIR=${DATA_ROOT}"
ExecStart=${GUARD_BIN}${GUARD_SCRIPT:+ ${GUARD_SCRIPT}} serve-discord-bot
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
