#!/usr/bin/env bash
set -euo pipefail

production_die() {
  printf 'production deployment error: %s\n' "$1" >&2
  exit 1
}

production_require_command() {
  command -v "$1" >/dev/null 2>&1 || production_die "$1 is required"
}

production_env_value() {
  local key="$1" file="$2" line
  line="$(awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$file")"
  printf '%s' "$line"
}

production_digest_is_valid() {
  [[ "$1" =~ ^ghcr\.io/[^/@[:space:]]+/[^/@[:space:]]+@sha256:[0-9a-fA-F]{64}$ ]]
}

production_validate_digest() {
  local name="$1" value="$2"
  production_digest_is_valid "$value" || production_die "$name must be a GHCR image pinned by a 64-hex SHA-256 digest"
}

production_validate_secret_file() {
  local name="$1" path="$2" mode owner
  [[ -n "$path" ]] || production_die "$name secret file path is empty"
  [[ -f "$path" ]] || production_die "$name secret file is missing"
  [[ ! -L "$path" ]] || production_die "$name secret file must not be a symlink"
  [[ -s "$path" ]] || production_die "$name secret file is empty"
  mode="$(stat -c '%a' "$path")"
  [[ "$mode" == "600" || "$mode" == "400" ]] || production_die "$name secret file must have mode 0600 or 0400"
  owner="$(stat -c '%u' "$path")"
  [[ "$owner" == "0" || "$owner" == "$(id -u)" ]] || production_die "$name secret file has an unsafe owner"
}

production_require_linux() {
  [[ "$(uname -s)" == "Linux" ]] || production_die "production scripts require Linux"
}

production_compose() {
  docker compose --env-file "$1" -f "$2" -p "$3" "${@:4}"
}
