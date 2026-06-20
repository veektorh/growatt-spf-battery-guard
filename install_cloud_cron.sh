#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT}/.venv/bin/python"
SCHEDULE_FILE="${ROOT}/schedule.json"
CRON_FILE="$(mktemp)"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Virtual environment not found at ${PYTHON_BIN}"
  echo "Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

if [[ ! -f "${SCHEDULE_FILE}" ]]; then
  echo "Schedule file not found at ${SCHEDULE_FILE}"
  exit 1
fi

crontab -l 2>/dev/null \
  | grep -v "# growatt-power-guard" \
  | grep -v "^CRON_TZ=Africa/Lagos$" > "${CRON_FILE}" || true

"${PYTHON_BIN}" - "${SCHEDULE_FILE}" "${ROOT}" "${PYTHON_BIN}" >> "${CRON_FILE}" <<'PY'
import json
import shlex
import sys
from pathlib import Path

schedule_path = Path(sys.argv[1])
root = sys.argv[2]
python_bin = sys.argv[3]

schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
timezone = schedule.get("timezone", "Africa/Lagos")
jobs = schedule.get("jobs", [])
if not isinstance(jobs, list) or not jobs:
    raise SystemExit("schedule.json must contain at least one job")

print(f"CRON_TZ={timezone}")
for job in jobs:
    job_id = job["id"].strip()
    cron = job["cron"].strip()
    command = job["command"].strip()
    args = job.get("args", [])
    if args is None:
        args = []
    if not isinstance(args, list) or not all(isinstance(arg, str) and arg.strip() for arg in args):
        raise SystemExit(f"Invalid schedule job args: {job}")
    if not job_id or not cron or not command:
        raise SystemExit(f"Invalid schedule job: {job}")
    print(
        f"{cron} cd {shlex.quote(root)} && {shlex.quote(python_bin)} "
        f"growatt_power_guard.py run-scheduled {shlex.quote(job_id)} >> "
        f"{shlex.quote(str(Path(root) / 'logs' / 'cron.log'))} 2>&1 # growatt-power-guard"
    )
PY

crontab "${CRON_FILE}"
rm -f "${CRON_FILE}"

mkdir -p "${ROOT}/logs"
echo "Installed Growatt cron schedule from ${SCHEDULE_FILE}."
crontab -l | grep "growatt-power-guard" || true
