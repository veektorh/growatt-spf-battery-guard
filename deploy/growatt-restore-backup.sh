#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Growatt restore verification must run as root." >&2
  exit 1
fi
if [[ "$#" -ne 2 ]]; then
  echo "Usage: $0 /path/to/growatt-backup.json.gpg /new/scratch/directory" >&2
  exit 1
fi

readonly encrypted="$(realpath "$1")"
readonly target="$(realpath -m "$2")"
readonly backup_environment="${GROWATT_BACKUP_ENV:-/etc/growatt-guard/backup.env}"
readonly runtime_root="${GROWATT_GUARD_RUNTIME_ROOT:-/home/ubuntu/automation/.deploy/current}"

case "${target}" in
  /|/etc|/home|/home/ubuntu|/home/ubuntu/automation|/home/ubuntu/automation/*|/opt|/opt/growatt-guard|/var|/var/*)
    echo "Refusing a live or system restore target." >&2
    exit 1
    ;;
esac
if [[ -e "${target}" ]]; then
  echo "The scratch restore target must not already exist." >&2
  exit 1
fi
if [[ ! -r "${encrypted}" || ! -r "${backup_environment}" ]]; then
  echo "The encrypted backup or root-owned backup configuration is unavailable." >&2
  exit 1
fi
if [[ ! -x "${runtime_root}/growatt-guard" ]]; then
  echo "The packaged Growatt runtime is unavailable." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${backup_environment}"
set +a
: "${BACKUP_ENCRYPTION_KEY:?BACKUP_ENCRYPTION_KEY is required}"

readonly plaintext="$(mktemp)"
readonly gpg_home="$(mktemp -d)"
chmod 0700 "${gpg_home}"
restore_complete=false
cleanup() {
  rm -f -- "${plaintext}"
  rm -rf -- "${gpg_home}"
  if [[ "${restore_complete}" != true ]]; then
    rm -rf -- "${target}"
  fi
}
trap cleanup EXIT

rm -f -- "${plaintext}"
/usr/bin/gpg --batch --quiet --homedir "${gpg_home}" \
  --passphrase-fd 3 --pinentry-mode loopback \
  --decrypt --output "${plaintext}" "${encrypted}" \
  3< <(printf '%s' "${BACKUP_ENCRYPTION_KEY}")
test -s "${plaintext}"

"${runtime_root}/.venv/bin/python" - "${plaintext}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("schema_version") != 1:
    raise SystemExit("Unexpected Growatt backup schema version.")
sections = payload.get("sections")
if not isinstance(sections, dict) or not sections:
    raise SystemExit("Growatt backup has no recoverable sections.")
if "utility_hold" in sections or payload.get("includes_active_hold"):
    raise SystemExit("Scheduled Growatt backups must not contain active Utility ownership.")
PY

install -d -m 0700 -o root -g root "${target}"
env \
  GROWATT_GUARD_HOME="${runtime_root}" \
  GROWATT_GUARD_DATA_DIR="${target}" \
  GROWATT_GUARD_STATE_DIR="${target}/state" \
  GROWATT_USERNAME=placeholder \
  GROWATT_PASSWORD=placeholder \
  GROWATT_SERVER_URL=https://restore-rehearsal.invalid/ \
  DRY_RUN=true \
  "${runtime_root}/growatt-guard" restore-state "${plaintext}"

restore_complete=true
trap - EXIT
rm -f -- "${plaintext}"
rm -rf -- "${gpg_home}"
echo "Verified isolated Growatt restore at ${target}."
