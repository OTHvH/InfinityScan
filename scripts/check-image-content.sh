#!/usr/bin/env bash
set -euo pipefail

image="${1:-}"
expected_user="${2:-}"
[[ -n "$image" ]] || { printf 'usage: %s IMAGE [EXPECTED_USER]\n' "$0" >&2; exit 2; }

actual_user="$(docker image inspect --format '{{.Config.User}}' "$image")"
if [[ -n "$expected_user" && "$actual_user" != "$expected_user" ]]; then
  printf 'image %s uses user %s, expected %s\n' "$image" "$actual_user" "$expected_user" >&2
  exit 1
fi
[[ -n "$actual_user" && "$actual_user" != "0" ]] || {
  printf 'image %s must run as a non-root user\n' "$image" >&2
  exit 1
}

docker run --rm --entrypoint /bin/sh "$image" -c '
  set -eu
  test ! -e /app/.env
  test ! -e /app/.env.production
  test ! -e /app/id_rsa
  test ! -e /app/identity.age
  ! find /app -type f \( -name "*.env" -o -name "*.pem" -o -name "*.key" -o -name "*.age" \) -print -quit | grep -q .
  ! find /app -type d \( -name tests -o -name "tests_*" -o -name e2e -o -name "test-results" -o -name "playwright-report" \) -print -quit | grep -q .
  ! grep -RIE --exclude="*.pyc" "(GITHUB_TOKEN|DATABASE_URL=|JWT_SECRET_KEY=|CSRF_SECRET_KEY=|AWS_SECRET_ACCESS_KEY=|R2_SECRET_ACCESS_KEY=)" /app 2>/dev/null
'

if docker image history --no-trunc "$image" | grep -Eiq '(GITHUB_TOKEN|DATABASE_URL=|JWT_SECRET_KEY=|CSRF_SECRET_KEY=|AWS_SECRET_ACCESS_KEY=|R2_SECRET_ACCESS_KEY=)'; then
  printf 'image history contains a secret-looking build value: %s\n' "$image" >&2
  exit 1
fi

printf 'runtime image content is safe: %s\n' "$image"
