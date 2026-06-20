#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT}/.venv/bin/python"
NOTIFY=1

if [[ "${1:-}" == "--no-notify" ]]; then
  NOTIFY=0
elif [[ "${1:-}" != "" ]]; then
  echo "Usage: ./update_server.sh [--no-notify]"
  exit 2
fi

cd "${ROOT}"

echo "Pulling latest code..."
git pull --ff-only

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv "${ROOT}/.venv"
fi

echo "Installing dependencies..."
"${PYTHON_BIN}" -m pip install -r requirements.txt

echo "Checking syntax..."
"${PYTHON_BIN}" -m py_compile "${ROOT}"/growatt_power_guard.py "${ROOT}"/growatt_guard/*.py

echo "Running tests..."
"${PYTHON_BIN}" -m unittest discover -s tests -q

echo "Validating schedule..."
"${PYTHON_BIN}" growatt_power_guard.py validate-schedule

echo "Installing cron schedule..."
./install_cloud_cron.sh

echo "Restarting long-lived processes..."
DASHBOARD_REFRESH_PID=$(pgrep -f "growatt_power_guard.py dashboard-refresh" || true)
if [[ -n "${DASHBOARD_REFRESH_PID}" ]]; then
  kill "${DASHBOARD_REFRESH_PID}"
  echo "Stopped dashboard-refresh (PID ${DASHBOARD_REFRESH_PID})."
fi
nohup "${PYTHON_BIN}" growatt_power_guard.py dashboard-refresh --interval-minutes 10 >> "${ROOT}/logs/cron.log" 2>&1 &
echo "Started dashboard-refresh (PID $!)."

echo "Running post-deploy smoke checks..."
"${PYTHON_BIN}" growatt_power_guard.py dashboard-refresh --once
"${PYTHON_BIN}" growatt_power_guard.py dashboard-stale-alert

echo "Running health check..."
if [[ "${NOTIFY}" == "1" ]]; then
  "${PYTHON_BIN}" growatt_power_guard.py health-check --notify
else
  "${PYTHON_BIN}" growatt_power_guard.py health-check
fi

echo "Server update complete."
