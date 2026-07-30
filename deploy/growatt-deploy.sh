#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: growatt-deploy <40-character-release-sha>" >&2
}

if [[ "$#" -ne 1 || ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  usage
  exit 64
fi

release_sha="$1"
deploy_root="${GROWATT_DEPLOY_ROOT:-${HOME}/automation}"

if [[ ! -d "${deploy_root}/.git" || ! -x "${deploy_root}/update_server.sh" ]]; then
  echo "Growatt deployment controller is not available at ${deploy_root}." >&2
  exit 1
fi

cd "${deploy_root}"
git fetch --quiet origin main

verified_sha="$(git rev-parse origin/main)"
if [[ "${verified_sha}" != "${release_sha}" ]]; then
  echo "Verified origin/main ${verified_sha} does not match requested release ${release_sha}." >&2
  exit 1
fi

exec ./update_server.sh \
  --no-notify \
  --wait-for-clear 15 \
  --release-sha "${release_sha}"
