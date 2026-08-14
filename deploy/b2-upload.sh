#!/usr/bin/env bash

# Sourced by growatt-backup.sh. Uses the native Backblaze B2 API so the
# production host does not need rclone or an additional SDK.

b2_json_field() {
  grep -o "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" <<< "$1" \
    | head -n1 \
    | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' \
    || true
}

b2_request() {
  local stage="$1"
  shift

  local response
  if ! response="$(curl \
    --silent \
    --show-error \
    --fail \
    --retry 3 \
    --retry-delay 2 \
    --retry-max-time 60 \
    --retry-connrefused \
    "$@")"; then
    echo "Backblaze B2 ${stage} failed after retries." >&2
    return 1
  fi

  printf '%s' "${response}"
}

b2_upload() {
  local file="$1"
  local remote_name="$2"
  local auth_json api_url auth_token bucket_id account_id

  auth_json="$(b2_request "account authorization" \
    -u "${B2_KEY_ID}:${B2_APPLICATION_KEY}" \
    https://api.backblazeb2.com/b2api/v3/b2_authorize_account)"
  api_url="$(b2_json_field "${auth_json}" apiUrl)"
  auth_token="$(b2_json_field "${auth_json}" authorizationToken)"
  bucket_id="$(b2_json_field "${auth_json}" bucketId)"
  if [[ -z "${api_url}" || -z "${auth_token}" ]]; then
    echo "Backblaze B2 authorization response was incomplete." >&2
    return 1
  fi

  if [[ -z "${bucket_id}" ]]; then
    account_id="$(b2_json_field "${auth_json}" accountId)"
    local list_json
    list_json="$(b2_request "bucket lookup" \
      -H "Authorization: ${auth_token}" \
      -d "{\"accountId\":\"${account_id}\",\"bucketName\":\"${B2_BUCKET_NAME}\"}" \
      "${api_url}/b2api/v3/b2_list_buckets")"
    bucket_id="$(b2_json_field "${list_json}" bucketId)"
  fi
  if [[ -z "${bucket_id}" ]]; then
    echo "Could not resolve the configured Backblaze B2 bucket." >&2
    return 1
  fi

  local upload_json upload_url upload_auth sha1 size response
  upload_json="$(b2_request "upload URL request" \
    -H "Authorization: ${auth_token}" \
    -d "{\"bucketId\":\"${bucket_id}\"}" \
    "${api_url}/b2api/v3/b2_get_upload_url")"
  upload_url="$(b2_json_field "${upload_json}" uploadUrl)"
  upload_auth="$(b2_json_field "${upload_json}" authorizationToken)"
  if [[ -z "${upload_url}" || -z "${upload_auth}" ]]; then
    echo "Backblaze B2 upload URL response was incomplete." >&2
    return 1
  fi

  sha1="$(sha1sum "${file}" | cut -d' ' -f1)"
  size="$(stat -c%s "${file}")"
  response="$(b2_request "file upload" \
    -X POST "${upload_url}" \
    -H "Authorization: ${upload_auth}" \
    -H "X-Bz-File-Name: ${remote_name}" \
    -H "Content-Type: application/octet-stream" \
    -H "X-Bz-Content-Sha1: ${sha1}" \
    -H "Content-Length: ${size}" \
    --data-binary "@${file}")"
  if [[ -z "$(b2_json_field "${response}" fileId)" ]]; then
    echo "Backblaze B2 upload response did not contain a file ID." >&2
    return 1
  fi
}
