#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/lib/production.sh"
MANIFEST=""
EVIDENCE=""
ENV_FILE="${STAGING_ENV_FILE:-/etc/infinityscan/staging.env}"
COMPOSE_FILE="${STAGING_COMPOSE_FILE:-$ROOT_DIR/infra/docker-compose.production.yml}"
SECRET_DIR="${STAGING_SECRET_DIR:-/etc/infinityscan/staging-secrets}"
HOST_ROOT="${DEPLOYMENT_HOST_ROOT:-/opt/infinityscan}"
INITIALIZE_MARKER=false
FAKE_HOST="${PREFLIGHT_FAKE_HOST:-false}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    --evidence) EVIDENCE="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --compose-file) COMPOSE_FILE="$2"; shift 2 ;;
    --secret-dir) SECRET_DIR="$2"; shift 2 ;;
    --host-root) HOST_ROOT="$2"; shift 2 ;;
    --initialize-marker) INITIALIZE_MARKER=true; shift ;;
    *) printf 'unknown preflight option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ -f "$MANIFEST" && -f "$EVIDENCE" && -f "$ENV_FILE" && -f "$COMPOSE_FILE" ]] || { printf 'manifest, evidence, environment, and Compose files are required\n' >&2; exit 1; }
[[ -d "$SECRET_DIR" ]] || production_die "staging secret directory is missing"
python3 "$ROOT_DIR/scripts/validate-staging-evidence.py" \
  --manifest "$MANIFEST" --evidence "$EVIDENCE" \
  --repository "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["repository"])' "$MANIFEST")" \
  --git-sha "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["git_sha"])' "$MANIFEST")" >/dev/null
[[ "$(production_env_value APP_ENV "$ENV_FILE")" == production ]] || production_die "staging runtime must use APP_ENV=production"
[[ "$(production_env_value DEPLOYMENT_ENVIRONMENT "$ENV_FILE")" == staging ]] || production_die "DEPLOYMENT_ENVIRONMENT=staging is required"
domain="$(production_env_value APP_DOMAIN "$ENV_FILE")"
[[ -n "$domain" && "$domain" != *production* && "$domain" != app.example.com ]] || production_die "staging domain is not isolated"
bucket="$(production_env_value S3_BUCKET "$ENV_FILE")"
[[ "$bucket" == *staging* ]] || production_die "staging object bucket must be explicitly staging-named"
backup_bucket="$(production_env_value BACKUP_BUCKET "$ENV_FILE")"
[[ "$backup_bucket" == *staging* ]] || production_die "staging backup bucket must be explicitly staging-named"
[[ "$(production_env_value S3_ENDPOINT_URL "$ENV_FILE")" == https://* ]] || production_die "staging object storage must use HTTPS"
[[ "$(production_env_value S3_ENDPOINT_URL "$ENV_FILE")" != *minio* ]] || production_die "MinIO is allowed only in local rehearsal mode"
if [[ "$(production_env_value ALLOW_SYNTHETIC_STAGING_BACKUP_BYPASS "$ENV_FILE")" == true ]]; then
  printf 'staging-only synthetic backup bypass is enabled; no production bypass is accepted\n'
fi
mkdir -p "$HOST_ROOT/state"
marker="$HOST_ROOT/state/environment.identity"
if [[ -f "$marker" ]]; then
  [[ "$(tr -d '[:space:]' <"$marker")" == staging ]] || production_die "deployment host identity is not staging"
elif [[ "$INITIALIZE_MARKER" == true ]]; then
  printf 'staging\n' >"$marker"
  chmod 0640 "$marker"
else
  production_die "staging environment marker is missing"
fi
for key in DATABASE_URL_FILE JWT_SECRET_KEY_FILE CSRF_SECRET_KEY_FILE S3_ACCESS_KEY_ID_FILE S3_SECRET_ACCESS_KEY_FILE; do
  path="$(production_env_value "$key" "$ENV_FILE")"
  [[ "$path" == "$SECRET_DIR"/* ]] || production_die "$key secret file is outside the staging secret directory"
  [[ -f "$path" && ! -L "$path" ]] || production_die "$key secret file is missing or symlinked"
  mode="$(stat -c '%a' "$path")"
  [[ "$mode" == 400 || "$mode" == 600 ]] || production_die "$key secret permissions are unsafe"
done
available_kb="$(df -Pk "$HOST_ROOT" | awk 'NR==2 {print $4}')"
[[ "$available_kb" =~ ^[0-9]+$ && "$available_kb" -ge 1048576 ]] || production_die "staging host requires 1 GiB free disk"
if [[ "$FAKE_HOST" != true ]]; then
  production_require_linux
  production_require_command docker
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config >/dev/null
fi
printf 'staging preflight passed for %s\n' "$domain"
