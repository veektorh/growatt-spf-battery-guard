#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sudo install -d -m 0755 -o root -g root /usr/local/libexec/growatt-guard
sudo install -d -m 0750 -o root -g root /etc/growatt-guard
sudo install -d -m 0700 -o root -g root /opt/growatt-guard/backups
sudo install -m 0755 -o root -g root "${ROOT}/deploy/growatt-backup.sh" \
  /usr/local/sbin/growatt-backup
sudo install -m 0755 -o root -g root "${ROOT}/deploy/growatt-restore-backup.sh" \
  /usr/local/sbin/growatt-restore-backup
sudo install -m 0644 -o root -g root "${ROOT}/deploy/b2-upload.sh" \
  /usr/local/libexec/growatt-guard/b2-upload.sh
sudo install -m 0644 -o root -g root "${ROOT}/deploy/growatt-backup.service" \
  /etc/systemd/system/growatt-backup.service
sudo install -m 0644 -o root -g root "${ROOT}/deploy/growatt-backup.timer" \
  /etc/systemd/system/growatt-backup.timer
sudo systemctl daemon-reload

if systemctl is-enabled --quiet growatt-backup.timer; then
  sudo systemctl restart growatt-backup.timer
  echo "Reinstalled and restarted growatt-backup.timer."
else
  echo "Installed Growatt backup units; verify a manual backup before enabling the timer."
fi
