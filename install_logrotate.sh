#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="/etc/logrotate.d/growatt-power-guard"
CRON_LOG="${ROOT}/logs/cron.log"
LOG_USER="$(stat -c '%U' "${ROOT}")"
LOG_GROUP="$(stat -c '%G' "${ROOT}")"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

mkdir -p "${ROOT}/logs"
cat > "${CONF}" <<CFG
${CRON_LOG} {
    su ${LOG_USER} ${LOG_GROUP}
    daily
    rotate 14
    maxsize 5M
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
CFG

echo "Installed logrotate config at ${CONF} for ${CRON_LOG} using su ${LOG_USER} ${LOG_GROUP}."
