#!/usr/bin/env bash
# Shared lifecycle helpers for disposable release verifiers.

if [ -n "${INFINITYSCAN_VERIFY_RUNTIME_LOADED:-}" ]; then
  return 0
fi
INFINITYSCAN_VERIFY_RUNTIME_LOADED=1

verify_runtime_env_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted { value=substr($0, index($0, "=") + 1) } END { print value }' "$VERIFY_ENV_FILE"
}

verify_runtime_set_env() {
  printf '%s=%s\n' "$1" "$2" >>"$VERIFY_ENV_FILE"
}

verify_runtime_refresh_compose() {
  VERIFY_COMPOSE=(
    env
    "POSTGRES_USER=$(verify_runtime_env_value POSTGRES_USER)"
    "POSTGRES_PASSWORD=$(verify_runtime_env_value POSTGRES_PASSWORD)"
    "POSTGRES_DB=$(verify_runtime_env_value POSTGRES_DB)"
    "DATABASE_URL=$(verify_runtime_env_value DATABASE_URL)"
    "JWT_SECRET_KEY=$(verify_runtime_env_value JWT_SECRET_KEY)"
    "CSRF_SECRET_KEY=$(verify_runtime_env_value CSRF_SECRET_KEY)"
    "MINIO_ROOT_USER=$(verify_runtime_env_value MINIO_ROOT_USER)"
    "MINIO_ROOT_PASSWORD=$(verify_runtime_env_value MINIO_ROOT_PASSWORD)"
    "S3_BUCKET=$(verify_runtime_env_value S3_BUCKET)"
    "API_PORT=$(verify_runtime_env_value API_PORT)"
    "WEB_PORT=$(verify_runtime_env_value WEB_PORT)"
    "MINIO_API_PORT=$(verify_runtime_env_value MINIO_API_PORT)"
    "MINIO_CONSOLE_PORT=$(verify_runtime_env_value MINIO_CONSOLE_PORT)"
    "INFINITYSCAN_ENV_FILE=$VERIFY_ENV_FILE"
    docker compose
    --project-name "$VERIFY_PROJECT"
    --env-file "$VERIFY_ENV_FILE"
    -f "$REPO_ROOT/infra/docker-compose.yml"
  )
}

verify_runtime_init() {
  local phase="$1" random_id repo_env
  umask 077
  random_id="$(od -An -N12 -tx1 /dev/urandom | tr -d '[:space:]')"
  phase="$(printf '%s' "$phase" | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9-')"
  # shellcheck disable=SC2034 # Public runtime state consumed by verifier scripts.
  VERIFY_RUN_ID="$random_id"
  VERIFY_PROJECT="infinityscan-${phase}-${random_id}"
  VERIFY_TMPDIR="$(mktemp -d "/tmp/infinityscan-${phase}-${random_id}-XXXXXX")"
  VERIFY_ENV_FILE="$VERIFY_TMPDIR/compose.env"
  VERIFY_DIAGNOSTICS_DIR="${VERIFY_DIAGNOSTICS_ROOT:-/tmp/infinityscan-verifier-diagnostics}/$VERIFY_PROJECT"
  VERIFY_OWNED_IMAGES=()
  VERIFY_OWNED_BUILDERS=()

  repo_env="$REPO_ROOT/infra/.env"
  VERIFY_REPO_ENV_PATH="$repo_env"
  if [ -e "$repo_env" ] || [ -L "$repo_env" ]; then
    VERIFY_REPO_ENV_EXISTED=true
    VERIFY_REPO_ENV_HASH="$(sha256sum -- "$repo_env" | awk '{print $1}')"
  else
    VERIFY_REPO_ENV_EXISTED=false
    VERIFY_REPO_ENV_HASH=""
  fi

  cp "$REPO_ROOT/infra/.env.example" "$VERIFY_ENV_FILE"
  chmod 600 "$VERIFY_ENV_FILE"
  awk '$0 !~ /^NEXT_PUBLIC_API_URL=/' "$VERIFY_ENV_FILE" >"$VERIFY_ENV_FILE.tmp"
  mv "$VERIFY_ENV_FILE.tmp" "$VERIFY_ENV_FILE"
  verify_runtime_set_env POSTGRES_USER "verify_${random_id}"
  verify_runtime_set_env POSTGRES_PASSWORD "verify_password_${random_id}"
  verify_runtime_set_env POSTGRES_DB "verify_${random_id}"
  verify_runtime_set_env DATABASE_URL "postgresql+psycopg://verify_${random_id}:verify_password_${random_id}@db:5432/verify_${random_id}"
  verify_runtime_set_env JWT_SECRET_KEY "verify_jwt_secret_${random_id}_0123456789"
  verify_runtime_set_env CSRF_SECRET_KEY "verify_csrf_secret_${random_id}_0123456789"
  verify_runtime_set_env ALLOWED_ORIGINS "http://localhost:*,http://127.0.0.1:*"
  verify_runtime_set_env TRUSTED_HOSTS "localhost,127.0.0.1,testserver,api"
  verify_runtime_set_env MINIO_ROOT_USER "verify_minio_${random_id}"
  verify_runtime_set_env MINIO_ROOT_PASSWORD "verify_minio_password_${random_id}"
  verify_runtime_set_env API_PORT 0
  verify_runtime_set_env WEB_PORT 0
  verify_runtime_set_env MINIO_API_PORT 0
  verify_runtime_set_env MINIO_CONSOLE_PORT 0

  export INFINITYSCAN_ENV_FILE="$VERIFY_ENV_FILE"
  export APP_ENV=development
  export OBJECT_STORAGE_ENABLED=false
  JWT_SECRET_KEY="$(verify_runtime_env_value JWT_SECRET_KEY)"
  CSRF_SECRET_KEY="$(verify_runtime_env_value CSRF_SECRET_KEY)"
  export JWT_SECRET_KEY CSRF_SECRET_KEY
  export API_INTERNAL_URL=http://127.0.0.1:8000
  unset DATABASE_URL NEXT_PUBLIC_API_URL S3_ENDPOINT_URL S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
  verify_runtime_refresh_compose
}

verify_runtime_preflight() {
  local path available_kb available_inodes docker_root
  local minimum_kb="${VERIFY_MIN_FREE_KIB:-5242880}"
  local minimum_inodes="${VERIFY_MIN_FREE_INODES:-100000}"
  docker_root="${VERIFY_DOCKER_FS_PATH:-$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)}"
  if [ -z "$docker_root" ] || [ ! -d "$docker_root" ]; then
    printf '%s\n' 'Verifier preflight could not identify Docker storage; set VERIFY_DOCKER_FS_PATH explicitly.' >&2
    return 1
  fi
  for path in "$REPO_ROOT" "$VERIFY_TMPDIR" "$docker_root"; do
    available_kb="$(df -Pk "$path" | awk 'NR == 2 { print $4 }')"
    available_inodes="$(df -Pi "$path" | awk 'NR == 2 { print $4 }')"
    if [ -z "$available_kb" ] || [ -z "$available_inodes" ] \
        || [ "$available_kb" -lt "$minimum_kb" ] \
        || [ "$available_inodes" -lt "$minimum_inodes" ]; then
      printf 'Verifier preflight failed for %s: available_kb=%s available_inodes=%s required_kb=%s required_inodes=%s\n' \
        "$path" "${available_kb:-unknown}" "${available_inodes:-unknown}" "$minimum_kb" "$minimum_inodes" >&2
      df -Pk "$path" >&2 || true
      df -Pi "$path" >&2 || true
      docker system df >&2 || true
      return 1
    fi
  done
}

verify_runtime_port() {
  local service="$1" container_port="$2" published
  published="$("${VERIFY_COMPOSE[@]}" port "$service" "$container_port" 2>/dev/null | awk 'NR == 1 { print }')"
  case "$published" in
    127.0.0.1:*|0.0.0.0:*|\[::\]:*) printf '%s\n' "${published##*:}" ;;
    *) return 1 ;;
  esac
}

verify_runtime_register_image() {
  VERIFY_OWNED_IMAGES+=("$1")
}

verify_runtime_register_builder() {
  VERIFY_OWNED_BUILDERS+=("$1")
}

verify_runtime_assert_repo_env_unchanged() {
  local current_hash
  if [ "$VERIFY_REPO_ENV_EXISTED" = true ]; then
    if [ ! -e "$VERIFY_REPO_ENV_PATH" ] && [ ! -L "$VERIFY_REPO_ENV_PATH" ]; then
      printf '%s\n' 'Repository infra/.env was removed by the verifier.' >&2
      return 1
    fi
    current_hash="$(sha256sum -- "$VERIFY_REPO_ENV_PATH" | awk '{print $1}')" || return 1
    if [ "$current_hash" != "$VERIFY_REPO_ENV_HASH" ]; then
      printf '%s\n' 'Repository infra/.env was modified by the verifier.' >&2
      return 1
    fi
  elif [ -e "$VERIFY_REPO_ENV_PATH" ] || [ -L "$VERIFY_REPO_ENV_PATH" ]; then
    printf '%s\n' 'Repository infra/.env was created by the verifier.' >&2
    return 1
  fi
}

verify_runtime_redact_file() {
  local source="$1" destination="$2"
  if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Diagnostic output omitted because the redactor is unavailable.' >"$destination"
    return 0
  fi
  python3 - "$VERIFY_ENV_FILE" "$source" "$destination" <<'PY'
import re
import sys
from pathlib import Path

env_path, source_path, destination_path = map(Path, sys.argv[1:])
secrets = []
for line in env_path.read_text(errors="replace").splitlines():
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if value and re.search(r"password|secret|token|credential|database_url|access_key", key, re.I):
        secrets.append(value)
text = source_path.read_text(errors="replace")
for secret in sorted(set(secrets), key=len, reverse=True):
    text = text.replace(secret, "[REDACTED]")
text = re.sub(r"(postgres(?:ql)?(?:\+[a-z0-9_]+)?://)[^\s:/@]+:[^\s/@]+@", r"\1[REDACTED]@", text, flags=re.I)
text = re.sub(r"((?:password|secret|token|credential|private[_-]?key)[a-z0-9_.-]*\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text, flags=re.I)
destination_path.write_text(text)
PY
}

verify_runtime_capture_diagnostics() {
  local raw
  command -v docker >/dev/null 2>&1 || return 0
  mkdir -p "$VERIFY_DIAGNOSTICS_DIR"
  chmod 700 "$VERIFY_DIAGNOSTICS_DIR"
  raw="$VERIFY_TMPDIR/compose-status.raw"
  if [ ! -e "$VERIFY_DIAGNOSTICS_DIR/compose-status.txt" ]; then
    "${VERIFY_COMPOSE[@]}" ps -a >"$raw" 2>&1 || true
    verify_runtime_redact_file "$raw" "$VERIFY_DIAGNOSTICS_DIR/compose-status.txt"
  fi
  raw="$VERIFY_TMPDIR/compose-logs.raw"
  if [ ! -e "$VERIFY_DIAGNOSTICS_DIR/compose-logs.txt" ]; then
    "${VERIFY_COMPOSE[@]}" logs --no-color --tail 200 >"$raw" 2>&1 || true
    verify_runtime_redact_file "$raw" "$VERIFY_DIAGNOSTICS_DIR/compose-logs.txt"
  fi
  printf 'project=%s\n' "$VERIFY_PROJECT" >"$VERIFY_DIAGNOSTICS_DIR/run.txt"
}

verify_runtime_project_empty() {
  [ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$1")" ]
}

verify_runtime_cleanup() {
  local status="$1" cleanup_failed=false item
  set +e
  if ! verify_runtime_assert_repo_env_unchanged; then
    status=1
  fi
  if [ "$status" -ne 0 ]; then
    verify_runtime_capture_diagnostics
  fi
  if command -v docker >/dev/null 2>&1; then
    "${VERIFY_COMPOSE[@]}" --profile storage down -v --remove-orphans --rmi local >/dev/null 2>&1 || cleanup_failed=true
    for item in "${VERIFY_OWNED_BUILDERS[@]}"; do
      docker buildx rm "$item" >/dev/null 2>&1 || cleanup_failed=true
    done
    for item in "${VERIFY_OWNED_IMAGES[@]}"; do
      docker image rm "$item" >/dev/null 2>&1 || cleanup_failed=true
    done
  fi
  if [ "$cleanup_failed" = true ]; then
    status=1
    verify_runtime_capture_diagnostics
  fi
  if ! verify_runtime_assert_repo_env_unchanged; then
    status=1
  fi
  rm -rf "$VERIFY_TMPDIR"
  if [ -d "$VERIFY_DIAGNOSTICS_DIR" ]; then
    printf 'Retained redacted diagnostics: %s\n' "$VERIFY_DIAGNOSTICS_DIR" >&2
  fi
  return "$status"
}
