#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT}/.venv/bin/python}"
SCHEDULE_FILE="${ROOT}/schedule.json"
CURRENT_CRON="$(mktemp)"
PROPOSED_CRON="$(mktemp)"
DRY_RUN=false

cleanup() {
  rm -f "${CURRENT_CRON}" "${PROPOSED_CRON}"
}
trap cleanup EXIT

usage() {
  cat <<EOF
Usage: ./install_cloud_cron.sh [--dry-run]

Install the Growatt cloud cron schedule from schedule.json.

Options:
  -n, --dry-run  Print the exact crontab diff without installing it.
  -h, --help     Show this help text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python interpreter not found or not executable: ${PYTHON_BIN}"
  echo "Set PYTHON_BIN to a valid interpreter, or run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

if [[ ! -f "${SCHEDULE_FILE}" ]]; then
  echo "Schedule file not found at ${SCHEDULE_FILE}"
  exit 1
fi

crontab -l > "${CURRENT_CRON}" 2>/dev/null || true

grep -v "# growatt-power-guard" "${CURRENT_CRON}" \
  | grep -v "^CRON_TZ=Africa/Lagos$" > "${PROPOSED_CRON}" || true

"${PYTHON_BIN}" - "${SCHEDULE_FILE}" "${ROOT}" "${PYTHON_BIN}" >> "${PROPOSED_CRON}" <<'PYCRON'
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
PYCRON

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "Dry run only. No crontab changes were installed."
  echo "Proposed crontab diff:"
  if command -v diff >/dev/null 2>&1; then
    diff -u --label current-crontab "${CURRENT_CRON}" --label proposed-crontab "${PROPOSED_CRON}" || true
  else
    echo "diff command not found; full proposed crontab follows:"
    cat "${PROPOSED_CRON}"
  fi
  exit 0
fi

crontab "${PROPOSED_CRON}"

mkdir -p "${ROOT}/logs"
echo "Installed Growatt cron schedule from ${SCHEDULE_FILE}."
crontab -l | grep "growatt-power-guard" || true
