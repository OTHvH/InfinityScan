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
done

curl "${curl_args[@]}" "$BASE_URL/api/series" >/dev/null

if [[ "$MODE" == staging ]]; then
  [[ "$BASE_URL" == https://* || "${SMOKE_ALLOW_HTTP_LOCAL:-false}" == true ]] || {
    printf 'staging smoke tests require HTTPS outside explicit local mode\n' >&2
    exit 1
  }
  username="${SMOKE_USERNAME:-}"
  password="${SMOKE_PASSWORD:-}"
  [[ -n "$username" && -n "$password" ]] || { printf 'staging smoke credentials are required\n' >&2; exit 1; }
  cookie_jar="${SMOKE_COOKIE_JAR:-$(mktemp)}"
  cleanup_cookie=false
  [[ -n "${SMOKE_COOKIE_JAR:-}" ]] || cleanup_cookie=true
  trap 'if [[ "$cleanup_cookie" == true ]]; then rm -f -- "$cookie_jar"; fi' EXIT
  csrf_payload="$(curl "${curl_args[@]}" -c "$cookie_jar" "$BASE_URL/api/auth/csrf")"
  csrf="$(printf '%s' "$csrf_payload" | jq -er '.csrf_token')"
  login_payload="$(curl "${curl_args[@]}" -b "$cookie_jar" -c "$cookie_jar" \
    -H 'Content-Type: application/json' -H "X-CSRF-Token: $csrf" \
    --data "$(jq -cn --arg username "$username" --arg password "$password" '{username:$username,password:$password}')" \
    "$BASE_URL/api/auth/login")"
  printf '%s' "$login_payload" | jq -e 'has("access_token") | not' >/dev/null
  curl "${curl_args[@]}" -b "$cookie_jar" "$BASE_URL/api/auth/me" >/dev/null
  rotated_csrf="$(curl "${curl_args[@]}" -b "$cookie_jar" -c "$cookie_jar" "$BASE_URL/api/auth/csrf" | jq -er '.csrf_token')"
  curl "${curl_args[@]}" -b "$cookie_jar" -c "$cookie_jar" -H "X-CSRF-Token: $rotated_csrf" -X POST "$BASE_URL/api/auth/refresh" >/dev/null
  curl "${curl_args[@]}" -b "$cookie_jar" -c "$cookie_jar" -H "X-CSRF-Token: $rotated_csrf" -X POST "$BASE_URL/api/auth/logout" >/dev/null
  logged_out_status="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
    -o /dev/null -w '%{http_code}' -b "$cookie_jar" "$BASE_URL/api/auth/me" || true)"
  if [[ "$logged_out_status" != 4* ]]; then
    printf 'logged-out smoke request unexpectedly succeeded\n' >&2
    exit 1
  fi
  if [[ -n "${SMOKE_SERIES_SLUG:-}" ]]; then
    curl "${curl_args[@]}" "$BASE_URL/api/library/${SMOKE_SERIES_SLUG}" >/dev/null
  fi
  if [[ -n "${SMOKE_MEDIA_PATH:-}" ]]; then
    [[ "$SMOKE_MEDIA_PATH" == /* && "$SMOKE_MEDIA_PATH" != *"?"* ]] || { printf 'invalid smoke media path\n' >&2; exit 1; }
    media_type="$(curl "${curl_args[@]}" -L -o /dev/null -w '%{content_type}' "$BASE_URL${SMOKE_MEDIA_PATH}")"
    [[ "$media_type" == image/* ]] || { printf 'synthetic media did not resolve to an image\n' >&2; exit 1; }
  fi
  headers_secure="$(curl "${curl_args[@]}" -D - -o /dev/null "$BASE_URL/api/auth/csrf")"
  printf '%s\n' "$headers_secure" | grep -qi '^set-cookie:.*Secure' || { printf 'staging cookies are not Secure\n' >&2; exit 1; }
  origin_status="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
    -o /dev/null -w '%{http_code}' -H 'Origin: https://evil.example.invalid' \
    "$BASE_URL/api/auth/csrf" || true)"
  if [[ "$origin_status" != 4* ]]; then
    printf 'untrusted Origin was accepted\n' >&2
    exit 1
  fi
fi
if [[ -n "${DIRECT_API_URL:-}" ]]; then
  if curl --silent --show-error --connect-timeout 2 --max-time 5 "$DIRECT_API_URL/livez" >/dev/null 2>&1; then
    printf 'direct API host port is reachable; production requires it to be internal\n' >&2
    exit 1
  fi
fi
printf 'deployment smoke tests passed for %s\n' "$BASE_URL"
