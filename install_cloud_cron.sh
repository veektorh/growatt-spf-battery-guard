#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT}/.venv/bin/python"
CRON_FILE="$(mktemp)"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Virtual environment not found at ${PYTHON_BIN}"
  echo "Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

crontab -l 2>/dev/null | grep -v "# growatt-power-guard" > "${CRON_FILE}" || true

cat >> "${CRON_FILE}" <<EOF
CRON_TZ=Africa/Lagos
30 6 * * * cd "${ROOT}" && "${PYTHON_BIN}" growatt_power_guard.py preserve-battery >> "${ROOT}/logs/cron.log" 2>&1 # growatt-power-guard
55 7 * * * cd "${ROOT}" && "${PYTHON_BIN}" growatt_power_guard.py return-sbu >> "${ROOT}/logs/cron.log" 2>&1 # growatt-power-guard
1 8 * * * cd "${ROOT}" && "${PYTHON_BIN}" growatt_power_guard.py watchdog-sbu >> "${ROOT}/logs/cron.log" 2>&1 # growatt-power-guard
30 14 * * 1-5 cd "${ROOT}" && "${PYTHON_BIN}" growatt_power_guard.py preserve-battery >> "${ROOT}/logs/cron.log" 2>&1 # growatt-power-guard
25 15 * * 1-5 cd "${ROOT}" && "${PYTHON_BIN}" growatt_power_guard.py return-sbu >> "${ROOT}/logs/cron.log" 2>&1 # growatt-power-guard
31 15 * * 1-5 cd "${ROOT}" && "${PYTHON_BIN}" growatt_power_guard.py watchdog-sbu >> "${ROOT}/logs/cron.log" 2>&1 # growatt-power-guard
0 21 * * * cd "${ROOT}" && "${PYTHON_BIN}" growatt_power_guard.py daily-summary >> "${ROOT}/logs/cron.log" 2>&1 # growatt-power-guard
10 0 * * * cd "${ROOT}" && "${PYTHON_BIN}" growatt_power_guard.py rotate-logs >> "${ROOT}/logs/cron.log" 2>&1 # growatt-power-guard
EOF

crontab "${CRON_FILE}"
rm -f "${CRON_FILE}"

mkdir -p "${ROOT}/logs"
echo "Installed Growatt cron schedule in Africa/Lagos timezone."
crontab -l | grep "growatt-power-guard" || true
