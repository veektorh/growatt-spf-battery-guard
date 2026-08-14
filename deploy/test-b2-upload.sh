#!/usr/bin/env bash
set -euo pipefail

readonly script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly work_directory="$(mktemp -d)"
trap 'rm -rf "${work_directory}"' EXIT

# shellcheck disable=SC1091
source "${script_directory}/b2-upload.sh"

export B2_KEY_ID="test-key-id"
export B2_APPLICATION_KEY="test-application-key"
export B2_BUCKET_NAME="test-bucket"

readonly curl_log="${work_directory}/curl.log"
curl() {
  printf '%q ' "$@" >> "${curl_log}"
  printf '\n' >> "${curl_log}"

  case "$*" in
    *"b2_authorize_account"*)
      printf '{"apiUrl":"https://api.example.test","authorizationToken":"auth-token","bucketId":"bucket-id"}'
      ;;
    *"b2_get_upload_url"*)
      printf '{"uploadUrl":"https://upload.example.test","authorizationToken":"upload-token"}'
      ;;
    *"https://upload.example.test"*)
      printf '{"fileId":"file-id"}'
      ;;
    *)
      return 2
      ;;
  esac
}

backup_file="${work_directory}/growatt.backup.json.gpg"
printf 'encrypted-backup' > "${backup_file}"
b2_upload "${backup_file}" "growatt-test.backup.json.gpg"

if [[ "$(wc -l < "${curl_log}")" -ne 3 ]]; then
  echo "Expected authorization, upload URL, and file upload requests." >&2
  exit 1
fi
while IFS= read -r request; do
  for required in "--fail" "--retry 3" "--retry-delay 2" "--retry-max-time 60"; do
    if [[ " ${request} " != *" ${required} "* ]]; then
      echo "Request is missing retry option: ${required}" >&2
      exit 1
    fi
  done
done < "${curl_log}"

curl() {
  return 22
}
if b2_request "file upload" https://upload.example.test 2> "${work_directory}/failure.log"; then
  echo "Expected the failed request to return non-zero." >&2
  exit 1
fi
grep -Fq "Backblaze B2 file upload failed after retries." "${work_directory}/failure.log"

curl() {
  printf '{}'
}
if b2_upload "${backup_file}" "growatt-test.backup.json.gpg" \
  2> "${work_directory}/incomplete.log"; then
  echo "Expected an incomplete authorization response to fail." >&2
  exit 1
fi
grep -Fq "Backblaze B2 authorization response was incomplete." \
  "${work_directory}/incomplete.log"

echo "B2 upload helper tests passed."
