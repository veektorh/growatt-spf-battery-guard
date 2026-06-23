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

if [[ -f "${ROOT}/state/topup_active.json" ]]; then
  if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Active topup state found, but ${PYTHON_BIN} is unavailable."
    echo "Refusing update to avoid leaving the inverter on Utility. Retry after the topup completes."
    exit 1
  fi

  echo "Active topup state found; attempting to complete it before updating..."
  "${PYTHON_BIN}" growatt_power_guard.py topup-complete-check || true
  if [[ -f "${ROOT}/state/topup_active.json" ]]; then
    echo "Topup is still active. Retry the update after it completes, or cancel the topup first."
    exit 1
  fi
fi

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
DASHBOARD_SERVICE=0
if command -v systemctl >/dev/null 2>&1 && systemctl cat growatt-dashboard-refresh.service >/dev/null 2>&1; then
  DASHBOARD_SERVICE=1
  sudo systemctl stop growatt-dashboard-refresh.service || true
fi
if pkill -f "growatt_power_guard.py dashboard-refresh" 2>/dev/null; then
  echo "Stopped legacy dashboard-refresh process."
fi
if [[ "${DASHBOARD_SERVICE}" == "0" ]] && pkill -f "growatt_power_guard.py observability-refresh" 2>/dev/null; then
  echo "Stopped observability-refresh."
fi
if [[ "${DASHBOARD_SERVICE}" == "1" ]]; then
  echo "Reinstalling dashboard services..."
  ./install_dashboard_service.sh
else
  nohup "${PYTHON_BIN}" growatt_power_guard.py observability-refresh --loop --interval-minutes 15 >> "${ROOT}/logs/cron.log" 2>&1 &
  echo "Started observability-refresh (PID $!)."
fi
if command -v systemctl >/dev/null 2>&1 && systemctl cat growatt-discord-control.service >/dev/null 2>&1; then
  echo "Restarting Discord control bot..."
  sudo systemctl restart growatt-discord-control.service
fi

echo "Running post-deploy smoke checks..."
"${PYTHON_BIN}" growatt_power_guard.py observability-refresh || echo "  (smoke check non-fatal — see log above)"
"${PYTHON_BIN}" growatt_power_guard.py dashboard-refresh --once || echo "  (smoke check non-fatal — see log above)"
"${PYTHON_BIN}" growatt_power_guard.py dashboard-stale-alert || echo "  (smoke check non-fatal — see log above)"

echo "Running health check..."
if [[ "${NOTIFY}" == "1" ]]; then
  "${PYTHON_BIN}" growatt_power_guard.py health-check --notify
else
  "${PYTHON_BIN}" growatt_power_guard.py health-check
fi

echo "Server update complete."
