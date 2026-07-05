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

print_preflight() {
  echo "Preflight:"
  echo "  Branch: $(git branch --show-current 2>/dev/null || echo unknown)"
  echo "  HEAD: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

  if [[ -x "${PYTHON_BIN}" ]]; then
    "${PYTHON_BIN}" - <<'PY'
import datetime as dt
import json
from pathlib import Path

root = Path.cwd()
state_dir = root / "state"

def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

for filename, label in (
    ("topup_active.json", "Topup"),
    ("utility_hold.json", "Utility hold"),
    ("automation_pause.json", "Pause"),
    ("mode_command.lock", "Command lock"),
):
    path = state_dir / filename
    state = read_json(path) if path.exists() else None
    if not path.exists():
        print(f"  {label}: clear")
        continue
    if state is None:
        print(f"  {label}: present but unreadable ({path})")
        continue
    summary = []
    for key in ("ownership", "target_soc", "max_expiry", "paused_until", "command", "created_at", "reason"):
        if state.get(key) not in (None, ""):
            summary.append(f"{key}={state[key]}")
    print(f"  {label}: present" + (f" ({'; '.join(summary)})" if summary else ""))

dashboard = read_json(root / "dashboard.json")
if dashboard and dashboard.get("generated_at"):
    try:
        generated = dt.datetime.fromisoformat(str(dashboard["generated_at"]))
        if generated.tzinfo is not None:
            generated = generated.astimezone().replace(tzinfo=None)
        age_min = max(0, (dt.datetime.now() - generated).total_seconds() / 60)
        print(f"  Dashboard age: {age_min:.0f} min")
    except ValueError:
        print("  Dashboard age: unknown")
else:
    print("  Dashboard age: unavailable")
PY
  else
    echo "  Python: unavailable (${PYTHON_BIN})"
  fi
}

print_preflight

if [[ -f "${ROOT}/state/topup_active.json" || -f "${ROOT}/state/utility_hold.json" ]]; then
  echo "Active topup/utility-hold state found. Refusing update to avoid interrupting inverter return-to-SBU automation."
  echo "Retry after the hold completes, or clear/cancel the hold intentionally before updating."
  exit 1
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
"${PYTHON_BIN}" tests/run_quiet.py

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
