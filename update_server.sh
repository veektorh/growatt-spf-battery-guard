#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_PYTHON="${ROOT}/.venv/bin/python"
DEPLOY_ROOT="${GROWATT_GUARD_DEPLOY_ROOT:-${ROOT}/.deploy}"
RELEASES_DIR="${DEPLOY_ROOT}/releases"
CURRENT_LINK="${DEPLOY_ROOT}/current"
RELEASE_KEEP_COUNT="${GROWATT_GUARD_RELEASE_KEEP_COUNT:-3}"
NOTIFY=1
WAIT_FOR_CLEAR_MINUTES=0

PREVIOUS_COMMIT=""
PREVIOUS_RELEASE=""
STAGING_DIR=""
BUILD_DIR=""
SOURCE_ROLLBACK_ARMED=0
ACTIVATION_ROLLBACK_ARMED=0

runtime_env() {
  env \
    GROWATT_GUARD_HOME="${CURRENT_LINK}" \
    GROWATT_GUARD_DATA_DIR="${ROOT}" \
    "$@"
}

install_runtime_integrations() {
  GROWATT_GUARD_RUNTIME_ROOT="${CURRENT_LINK}" \
    GROWATT_GUARD_DATA_DIR="${ROOT}" \
    "${ROOT}/install_cloud_cron.sh"
  GROWATT_GUARD_RUNTIME_ROOT="${CURRENT_LINK}" \
    GROWATT_GUARD_DATA_DIR="${ROOT}" \
    "${ROOT}/install_dashboard_service.sh"
  if systemctl cat growatt-discord-control.service >/dev/null 2>&1; then
    GROWATT_GUARD_RUNTIME_ROOT="${CURRENT_LINK}" \
      GROWATT_GUARD_DATA_DIR="${ROOT}" \
      "${ROOT}/install_discord_bot_service.sh"
  fi
}

restore_previous_release() {
  if [[ -n "${PREVIOUS_RELEASE}" && -d "${PREVIOUS_RELEASE}" ]]; then
    ln -sfn "${PREVIOUS_RELEASE}" "${CURRENT_LINK}.rollback"
    mv -Tf "${CURRENT_LINK}.rollback" "${CURRENT_LINK}"
    install_runtime_integrations
    echo "Restored packaged release ${PREVIOUS_RELEASE}." >&2
  else
    echo "No previous packaged release was available for automatic runtime rollback." >&2
  fi
}

rollback_deployment_failure() {
  local status=$?
  if [[ "${status}" -eq 0 ]]; then
    return 0
  fi

  trap - EXIT
  set +e
  if [[ "${SOURCE_ROLLBACK_ARMED}" == "1" && -n "${PREVIOUS_COMMIT}" ]]; then
    echo "Source validation/deployment failed; restoring checkout ${PREVIOUS_COMMIT}..." >&2
    git -C "${ROOT}" reset --hard "${PREVIOUS_COMMIT}"
  fi
  if [[ "${ACTIVATION_ROLLBACK_ARMED}" == "1" ]]; then
    echo "Packaged release activation failed; restoring the previous runtime..." >&2
    if [[ -n "${PREVIOUS_RELEASE}" && -d "${PREVIOUS_RELEASE}" ]]; then
      restore_previous_release
    else
      rm -f -- "${CURRENT_LINK}"
      PYTHON_BIN="${CONTROL_PYTHON}" "${ROOT}/install_cloud_cron.sh"
      "${ROOT}/install_dashboard_service.sh"
      if systemctl cat growatt-discord-control.service >/dev/null 2>&1; then
        "${ROOT}/install_discord_bot_service.sh"
      fi
      echo "Restored the legacy checkout runtime." >&2
    fi
  fi
  if [[ -n "${STAGING_DIR}" && -d "${STAGING_DIR}" ]]; then
    rm -rf -- "${STAGING_DIR}"
  fi
  if [[ -n "${BUILD_DIR}" && -d "${BUILD_DIR}" ]]; then
    rm -rf -- "${BUILD_DIR}"
  fi
  exit "${status}"
}

trap rollback_deployment_failure EXIT

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

if [[ ! "${RELEASE_KEEP_COUNT}" =~ ^[1-9][0-9]*$ ]]; then
  echo "GROWATT_GUARD_RELEASE_KEEP_COUNT must be a positive integer." >&2
  exit 2
fi

cd "${ROOT}"

if [[ ! -x "${CONTROL_PYTHON}" ]]; then
  echo "Creating deployment-controller virtual environment..."
  python3 -m venv "${ROOT}/.venv"
  "${CONTROL_PYTHON}" -m pip install -r "${ROOT}/requirements-build.lock"
  "${CONTROL_PYTHON}" -m pip install -r "${ROOT}/requirements.lock"
fi

run_deployment_preflight() {
  "${CONTROL_PYTHON}" growatt_power_guard.py deployment-preflight
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

echo "Pulling latest source..."
PREVIOUS_COMMIT="$(git rev-parse HEAD)"
git pull --ff-only
SOURCE_ROLLBACK_ARMED=1

echo "Synchronizing the pinned verification environment..."
"${CONTROL_PYTHON}" -m pip install -r "${ROOT}/requirements-build.lock"
"${CONTROL_PYTHON}" -m pip install -r "${ROOT}/requirements.lock"

echo "Checking source syntax..."
"${CONTROL_PYTHON}" -m py_compile "${ROOT}"/growatt_power_guard.py "${ROOT}"/growatt_guard/*.py

echo "Running offline tests..."
"${CONTROL_PYTHON}" tests/run_quiet.py

echo "Validating source schedule..."
"${CONTROL_PYTHON}" growatt_power_guard.py validate-schedule

release_id="$(git rev-parse --short=12 HEAD)"
release_path="${RELEASES_DIR}/${release_id}"
BUILD_DIR="$(mktemp -d)"

mkdir -p "${RELEASES_DIR}"
if [[ ! -d "${release_path}" ]]; then
  # Create the virtual environment at its final absolute path. Python venv
  # entry-point shebangs embed that path and break if the directory is renamed.
  STAGING_DIR="${release_path}"
  mkdir -p "${STAGING_DIR}"

  echo "Building wheel for ${release_id}..."
  "${CONTROL_PYTHON}" -m pip wheel --no-deps --no-build-isolation --wheel-dir "${BUILD_DIR}" "${ROOT}"
  wheel_files=("${BUILD_DIR}"/*.whl)
  if [[ ! -f "${wheel_files[0]}" || "${#wheel_files[@]}" -ne 1 ]]; then
    echo "Expected exactly one application wheel; found ${#wheel_files[@]}." >&2
    exit 1
  fi

  echo "Creating isolated packaged runtime..."
  python3 -m venv "${STAGING_DIR}/.venv"
  "${STAGING_DIR}/.venv/bin/python" -m pip install -r "${ROOT}/requirements.lock"
  "${STAGING_DIR}/.venv/bin/python" -m pip install --no-deps "${wheel_files[0]}"
  install -m 0644 "${ROOT}/schedule.json" "${STAGING_DIR}/schedule.json"
  install -m 0755 "${ROOT}/packaged_entrypoint.sh" "${STAGING_DIR}/growatt-guard"
  printf '%s\n' "${ROOT}" > "${STAGING_DIR}/DATA_ROOT"
  printf '%s\n' "${release_id}" > "${STAGING_DIR}/RELEASE"

  echo "Validating staged release..."
  env GROWATT_GUARD_HOME="${STAGING_DIR}" GROWATT_GUARD_DATA_DIR="${ROOT}" \
    "${STAGING_DIR}/growatt-guard" validate-schedule
  env GROWATT_GUARD_HOME="${STAGING_DIR}" GROWATT_GUARD_DATA_DIR="${ROOT}" \
    "${STAGING_DIR}/growatt-guard" --help >/dev/null

  STAGING_DIR=""
else
  echo "Reusing existing packaged release ${release_id}."
  if [[ "$(cat "${release_path}/RELEASE" 2>/dev/null || true)" != "${release_id}" ]]; then
    echo "Existing release directory is incomplete or has the wrong identity: ${release_path}" >&2
    exit 1
  fi
  env GROWATT_GUARD_HOME="${release_path}" GROWATT_GUARD_DATA_DIR="${ROOT}" \
    "${release_path}/growatt-guard" validate-schedule
fi

# A hold or command lock may have appeared while dependencies were being staged.
# Recheck immediately before changing the active runtime.
run_deployment_preflight

if [[ -L "${CURRENT_LINK}" ]]; then
  PREVIOUS_RELEASE="$(readlink -f "${CURRENT_LINK}")"
fi
ln -sfn "${release_path}" "${CURRENT_LINK}.new"
mv -Tf "${CURRENT_LINK}.new" "${CURRENT_LINK}"
ACTIVATION_ROLLBACK_ARMED=1

echo "Installing cron and restarting packaged services..."
install_runtime_integrations

echo "Running post-activation checks..."
runtime_env "${CURRENT_LINK}/growatt-guard" service-status --json
runtime_env "${CURRENT_LINK}/growatt-guard" dashboard-refresh --once \
  || echo "  (dashboard refresh non-fatal - see log above)"
runtime_env "${CURRENT_LINK}/growatt-guard" dashboard-stale-alert \
  || echo "  (stale check non-fatal - see log above)"

echo "Running health check..."
if [[ "${NOTIFY}" == "1" ]]; then
  runtime_env "${CURRENT_LINK}/growatt-guard" health-check --notify
else
  runtime_env "${CURRENT_LINK}/growatt-guard" health-check
fi

ACTIVATION_ROLLBACK_ARMED=0
SOURCE_ROLLBACK_ARMED=0

echo "Pruning old packaged releases (keeping ${RELEASE_KEEP_COUNT})..."
mapfile -t old_releases < <(
  find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -rn \
    | tail -n "+$((RELEASE_KEEP_COUNT + 1))" \
    | cut -d' ' -f2-
)
for old_release in "${old_releases[@]}"; do
  [[ -n "${old_release}" && "${old_release}" == "${RELEASES_DIR}/"* ]] || continue
  rm -rf -- "${old_release}"
done

rm -rf -- "${BUILD_DIR}"
BUILD_DIR=""
trap - EXIT
echo "Server update complete. Active packaged release: ${release_id}"
