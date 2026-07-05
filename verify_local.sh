#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ "$PYTHON_BIN" == ".venv/bin/python" && ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

echo "== Python compile =="
"$PYTHON_BIN" -m py_compile growatt_power_guard.py growatt_guard/*.py tests/*.py

echo "== Unit tests =="
"$PYTHON_BIN" tests/run_quiet.py

echo "== Schedule validation =="
"$PYTHON_BIN" growatt_power_guard.py validate-schedule

echo "== Whitespace check =="
git diff --check

echo "== Public hygiene check =="
"$PYTHON_BIN" scripts/check_public_hygiene.py
