#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Growatt backups must run through the root-owned systemd service." >&2
  exit 1
fi

readonly backup_environment="${GROWATT_BACKUP_ENV:-/etc/growatt-guard/backup.env}"
readonly runtime_root="${GROWATT_GUARD_RUNTIME_ROOT:-/home/ubuntu/automation/.deploy/current}"
readonly data_root="${GROWATT_GUARD_DATA_DIR:-/home/ubuntu/automation}"
readonly b2_helper="${GROWATT_B2_HELPER:-/usr/local/libexec/growatt-guard/b2-upload.sh}"
readonly restore_command="${GROWATT_RESTORE_COMMAND:-/usr/local/sbin/growatt-restore-backup}"

if [[ ! -r "${backup_environment}" ]]; then
  echo "Missing root-owned Growatt backup configuration." >&2
  exit 1
fi
if [[ ! -x "${runtime_root}/growatt-guard" || ! -r "${b2_helper}" || ! -x "${restore_command}" ]]; then
  echo "The packaged runtime or installed Growatt backup helpers are unavailable." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${backup_environment}"
set +a
: "${BACKUP_ENCRYPTION_KEY:?BACKUP_ENCRYPTION_KEY is required}"
: "${B2_KEY_ID:?B2_KEY_ID is required for off-site backups}"
: "${B2_APPLICATION_KEY:?B2_APPLICATION_KEY is required for off-site backups}"
: "${B2_BUCKET_NAME:?B2_BUCKET_NAME is required for off-site backups}"

readonly backup_dir="/opt/growatt-guard/backups"
readonly retention_days="${BACKUP_RETENTION_DAYS:-14}"
if [[ ! "${retention_days}" =~ ^[1-9][0-9]*$ ]]; then
  echo "BACKUP_RETENTION_DAYS must be a positive integer." >&2
  exit 1
fi

readonly timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
readonly encrypted="${backup_dir}/growatt-${timestamp}.backup.json.gpg"
install -d -m 0700 -o root -g root "${backup_dir}"
readonly plaintext="$(mktemp "${backup_dir}/.growatt-${timestamp}.XXXXXX.backup.json")"
readonly restore_target="$(mktemp -d "${backup_dir}/.restore-${timestamp}.XXXXXX")"
readonly gpg_home="$(mktemp -d "${backup_dir}/.gnupg-${timestamp}.XXXXXX")"
chmod 0700 "${gpg_home}"
rm -rf -- "${restore_target}"

backup_complete=false
cleanup() {
  rm -f -- "${plaintext}"
  rm -rf -- "${restore_target}"
  rm -rf -- "${gpg_home}"
  if [[ "${backup_complete}" != true ]]; then
    rm -f -- "${encrypted}"
  fi
}
trap cleanup EXIT

env \
  GROWATT_GUARD_HOME="${runtime_root}" \
  GROWATT_GUARD_DATA_DIR="${data_root}" \
  "${runtime_root}/growatt-guard" backup-state --output "${plaintext}"
test -s "${plaintext}"

/usr/bin/gpg --batch --yes --homedir "${gpg_home}" \
  --passphrase-fd 3 --pinentry-mode loopback \
  --symmetric --cipher-algo AES256 --output "${encrypted}" "${plaintext}" \
  3< <(printf '%s' "${BACKUP_ENCRYPTION_KEY}")
test -s "${encrypted}"
rm -f -- "${plaintext}"

"${restore_command}" "${encrypted}" "${restore_target}"
rm -rf -- "${restore_target}"
rm -rf -- "${gpg_home}"
backup_complete=true

# shellcheck disable=SC1090
source "${b2_helper}"
b2_upload "${encrypted}" "$(basename "${encrypted}")"

find "${backup_dir}" -type f -name 'growatt-*.backup.json.gpg' \
  -mtime "+${retention_days}" -delete

trap - EXIT
echo "Created, restore-verified, and uploaded $(basename "${encrypted}")."
