#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT}/.venv/bin/python"
NOTIFY=1
WAIT_FOR_CLEAR_MINUTES=0

PREVIOUS_COMMIT=""
ROLLBACK_ARMED=0

rollback_validation_failure() {
  local status=$?
  if [[ "${status}" -eq 0 || "${ROLLBACK_ARMED}" != "1" ]]; then
    return "${status}"
  fi

  echo "Deployment validation failed; rolling back to ${PREVIOUS_COMMIT}..." >&2
  trap - EXIT
  set +e
  git reset --hard "${PREVIOUS_COMMIT}"
  local rollback_status=$?
  if [[ "${rollback_status}" -ne 0 ]]; then
    echo "Rollback failed; repository may require manual recovery." >&2
  else
    echo "Rollback complete. No cron or long-lived process changes were made." >&2
  fi
  exit "${status}"
}

trap rollback_validation_failure EXIT

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --no-notify)
      NOTIFY=0
      shift
      ;;
    --wait-for-clear)
      if [[ "${2:-}" == "" || ! "${2:-}" =~ ^[1-9][0-9]*$ ]]; then
        echo "--wait-for-clear requires a positive number of minutes." >&2
        exit 2
      fi
      WAIT_FOR_CLEAR_MINUTES="$2"
      shift 2
      ;;
    *)
      echo "Usage: ./update_server.sh [--no-notify] [--wait-for-clear MINUTES]"
      exit 2
      ;;
  esac
done

cd "${ROOT}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv "${ROOT}/.venv"
fi

run_deployment_preflight() {
  "${PYTHON_BIN}" growatt_power_guard.py deployment-preflight
}

if ! run_deployment_preflight; then
  if [[ "${WAIT_FOR_CLEAR_MINUTES}" -eq 0 ]]; then
    exit 1
  fi
  wait_deadline=$(( $(date +%s) + WAIT_FOR_CLEAR_MINUTES * 60 ))
  preflight_clear=0
  echo "Deployment is currently blocked; waiting up to ${WAIT_FOR_CLEAR_MINUTES} minutes for a safe window..."
  while (( $(date +%s) < wait_deadline )); do
    sleep 60
    if run_deployment_preflight; then
      echo "Deployment preflight is clear; continuing."
      preflight_clear=1
      break
    fi
  done
  if [[ "${preflight_clear}" -ne 1 ]]; then
    echo "Deployment remained blocked after ${WAIT_FOR_CLEAR_MINUTES} minutes; no update was applied." >&2
    exit 1
  fi
fi

echo "Pulling latest code..."
PREVIOUS_COMMIT="$(git rev-parse HEAD)"
git pull --ff-only
ROLLBACK_ARMED=1

echo "Installing dependencies..."
"${PYTHON_BIN}" -m pip install -r requirements.txt

echo "Checking syntax..."
"${PYTHON_BIN}" -m py_compile "${ROOT}"/growatt_power_guard.py "${ROOT}"/growatt_guard/*.py

echo "Running tests..."
"${PYTHON_BIN}" tests/run_quiet.py

echo "Validating schedule..."
"${PYTHON_BIN}" growatt_power_guard.py validate-schedule
ROLLBACK_ARMED=0
trap - EXIT

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
if pkill -f "growatt_power_guard.py observability-refresh" 2>/dev/null; then
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

echo "Refreshing dashboard once..."
"${PYTHON_BIN}" growatt_power_guard.py dashboard-refresh --once || echo "  (dashboard refresh non-fatal - see log above)"

echo "Running post-deploy smoke checks..."
"${PYTHON_BIN}" growatt_power_guard.py service-status --json
if [[ "${RUN_PV_METRIC_PROBE:-false}" == "true" ]]; then
  "${PYTHON_BIN}" growatt_power_guard.py pv-metric-probe --json || echo "  (PV metric probe non-fatal - see log above)"
else
  echo "Skipping PV metric probe (set RUN_PV_METRIC_PROBE=true to include one extra Growatt read)."
fi
"${PYTHON_BIN}" growatt_power_guard.py dashboard-stale-alert || echo "  (smoke check non-fatal - see log above)"

echo "Running health check..."
if [[ "${NOTIFY}" == "1" ]]; then
  "${PYTHON_BIN}" growatt_power_guard.py health-check --notify
else
  "${PYTHON_BIN}" growatt_power_guard.py health-check
fi

echo "Server update complete."
