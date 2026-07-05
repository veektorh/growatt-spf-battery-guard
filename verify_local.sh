#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ "$PYTHON_BIN" == ".venv/bin/python" && ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

SECRET_PATTERN='GROWATT_USERNAME|GROWATT_PASSWORD|GROWATT_PLANT_ID|GROWATT_DEVICE_SN|discord.com/api/webhooks|WEATHER_LAT|WEATHER_LON'

echo "== Python compile =="
"$PYTHON_BIN" -m py_compile growatt_power_guard.py growatt_guard/*.py tests/*.py

echo "== Unit tests =="
"$PYTHON_BIN" tests/run_quiet.py

echo "== Schedule validation =="
"$PYTHON_BIN" growatt_power_guard.py validate-schedule

echo "== Whitespace check =="
git diff --check

echo "== Public secret-pattern scan =="
if rg -n "$SECRET_PATTERN" --glob "!verify_local.sh" .; then
  echo "Review the matches above; expected matches are placeholders, test fixtures, or redaction code."
else
  echo "No public secret-pattern matches found."
fi
