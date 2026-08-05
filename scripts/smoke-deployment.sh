#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-}"
MODE="${SMOKE_MODE:-staging}"
[[ -n "$BASE_URL" ]] || { printf 'usage: %s BASE_URL\n' "$0" >&2; exit 2; }
[[ "$BASE_URL" != */ ]] && : || BASE_URL="${BASE_URL%/}"
if [[ "$MODE" == production && "$BASE_URL" != https://* ]]; then
  printf 'production smoke tests require HTTPS\n' >&2
  exit 1
fi

curl_args=(--fail --silent --show-error --connect-timeout 3 --max-time 10)
check_status() {
  local path="$1" expected="${2:-200}" status
  status="$(curl "${curl_args[@]}" -o /dev/null -w '%{http_code}' "$BASE_URL$path")"
  [[ "$status" == "$expected" ]] || { printf 'smoke failure: %s returned %s\n' "$path" "$status" >&2; exit 1; }
}

check_status / 200
check_status /api/livez 200
check_status /api/readyz 200
check_status /api/health 200

if [[ -n "${SMOKE_ASSET_PATH:-}" ]]; then
  [[ "$SMOKE_ASSET_PATH" == /* && "$SMOKE_ASSET_PATH" != *"?"* ]] || { printf 'invalid smoke asset path\n' >&2; exit 1; }
  check_status "$SMOKE_ASSET_PATH" 200
fi

headers="$(curl "${curl_args[@]}" -D - -o /dev/null "$BASE_URL/")"
for header in x-content-type-options referrer-policy permissions-policy content-security-policy; do
  printf '%s\n' "$headers" | grep -qi "^$header:" || { printf 'missing security header: %s\n' "$header" >&2; exit 1; }

curl "${curl_args[@]}" "$BASE_URL/api/series" >/dev/null
if [[ -n "${DIRECT_API_URL:-}" ]]; then
  if curl --silent --show-error --connect-timeout 2 --max-time 5 "$DIRECT_API_URL/livez" >/dev/null 2>&1; then
    printf 'direct API host port is reachable; production requires it to be internal\n' >&2
    exit 1
  fi
fi
printf 'deployment smoke tests passed for %s\n' "$BASE_URL"
