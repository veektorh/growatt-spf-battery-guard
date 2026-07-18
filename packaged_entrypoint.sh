#!/usr/bin/env bash
set -euo pipefail

RUNTIME_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_ROOT_FILE="${RUNTIME_ROOT}/DATA_ROOT"

if [[ -z "${GROWATT_GUARD_DATA_DIR:-}" ]]; then
  if [[ ! -f "${DATA_ROOT_FILE}" ]]; then
    echo "Packaged release is missing ${DATA_ROOT_FILE}." >&2
    exit 1
  fi
  GROWATT_GUARD_DATA_DIR="$(<"${DATA_ROOT_FILE}")"
fi

export GROWATT_GUARD_HOME="${GROWATT_GUARD_HOME:-${RUNTIME_ROOT}}"
export GROWATT_GUARD_DATA_DIR
exec "${RUNTIME_ROOT}/.venv/bin/growatt-guard" "$@"
