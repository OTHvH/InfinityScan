#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
# shellcheck source=scripts/lib/production.sh
source "$ROOT_DIR/scripts/lib/production.sh"

RELEASE_ID=""
API_IMAGE="${API_IMAGE:-}"
WEB_IMAGE="${WEB_IMAGE:-}"
CADDY_IMAGE="${CADDY_IMAGE:-}"
RELEASE_MANIFEST_FILE="${RELEASE_MANIFEST_FILE:-}"
RELEASE_EVIDENCE_FILE="${RELEASE_EVIDENCE_FILE:-}"
ENV_FILE="${PRODUCTION_ENV_FILE:-/etc/infinityscan/production.env}"
SECRET_DIR="${PRODUCTION_SECRET_DIR:-/etc/infinityscan/secrets}"
DEPLOY_DIR="${DEPLOYMENT_DIR:-/opt/infinityscan}"
STATE_DIR="${DEPLOYMENT_STATE_DIR:-/var/lib/infinityscan/releases}"
LOCK_FILE="${DEPLOYMENT_LOCK_FILE:-/var/lib/infinityscan/deploy.lock}"
STAGING_MODE="${STAGING_MODE:-false}"
DRY_RUN="${DRY_RUN:-false}"
BACKUP_METADATA_FILE="${BACKUP_METADATA_FILE:-}"
BACKUP_MAX_AGE_SECONDS="${BACKUP_MAX_AGE_SECONDS:-86400}"
DEPLOYMENT_ENVIRONMENT="${DEPLOYMENT_ENVIRONMENT:-production}"
SYNTHETIC_STAGING_BACKUP_BYPASS=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) DEPLOYMENT_ENVIRONMENT="$2"; shift 2 ;;
    --release-manifest) RELEASE_MANIFEST_FILE="$2"; shift 2 ;;
    --release-evidence) RELEASE_EVIDENCE_FILE="$2"; shift 2 ;;
    --synthetic-staging-backup-bypass) SYNTHETIC_STAGING_BACKUP_BYPASS=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --*) production_die "unknown deployment option: $1" ;;
    *) [[ -z "$RELEASE_ID" ]] || production_die "only one release ID is allowed"; RELEASE_ID="$1"; shift ;;
  esac
done

if [[ "$DEPLOYMENT_ENVIRONMENT" == staging ]]; then
  STAGING_MODE=true
  ENV_FILE="${STAGING_ENV_FILE:-${PRODUCTION_ENV_FILE:-/etc/infinityscan/staging.env}}"
  SECRET_DIR="${STAGING_SECRET_DIR:-${PRODUCTION_SECRET_DIR:-/etc/infinityscan/staging-secrets}}"
  DEPLOY_DIR="${STAGING_DEPLOYMENT_DIR:-${DEPLOYMENT_DIR:-/opt/infinityscan}}"
  STATE_DIR="${STAGING_DEPLOYMENT_STATE_DIR:-${DEPLOYMENT_STATE_DIR:-/var/lib/infinityscan/staging/releases}}"
  LOCK_FILE="${STAGING_DEPLOYMENT_LOCK_FILE:-${DEPLOYMENT_LOCK_FILE:-/var/lib/infinityscan/staging/deploy.lock}}"
fi

production_require_linux
production_require_command docker
production_require_command python3
production_require_command flock
[[ -f "$ENV_FILE" ]] || production_die "production environment file is missing"
[[ "$(production_env_value APP_ENV "$ENV_FILE")" == production ]] || production_die "APP_ENV must be production"
[[ "$(production_env_value DEPLOYMENT_ENVIRONMENT "$ENV_FILE")" == "$DEPLOYMENT_ENVIRONMENT" ]] || production_die "deployment environment does not match the environment file"
if [[ -n "$RELEASE_MANIFEST_FILE" ]]; then
  [[ -f "$RELEASE_MANIFEST_FILE" ]] || production_die "release manifest is missing"
  read -r manifest_release manifest_api manifest_web < <(
    python3 "$ROOT_DIR/scripts/validate-release-manifest.py" "$RELEASE_MANIFEST_FILE" >/dev/null
    python3 - "$RELEASE_MANIFEST_FILE" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
print(manifest["release_id"], manifest["images"]["api"]["reference"], manifest["images"]["web"]["reference"])
PY
  )
  RELEASE_ID="${RELEASE_ID:-$manifest_release}"
  API_IMAGE="${API_IMAGE:-$manifest_api}"
  WEB_IMAGE="${WEB_IMAGE:-$manifest_web}"
fi
if [[ "$DEPLOYMENT_ENVIRONMENT" == staging ]]; then
  [[ -n "$RELEASE_MANIFEST_FILE" && -f "$RELEASE_MANIFEST_FILE" ]] || production_die "staging requires an exact release manifest"
  [[ -n "$RELEASE_EVIDENCE_FILE" && -f "$RELEASE_EVIDENCE_FILE" ]] || production_die "staging requires publication evidence"
  manifest_repository="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["repository"])' "$RELEASE_MANIFEST_FILE")"
  manifest_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["git_sha"])' "$RELEASE_MANIFEST_FILE")"
  python3 "$ROOT_DIR/scripts/validate-staging-evidence.py" \
    --manifest "$RELEASE_MANIFEST_FILE" --evidence "$RELEASE_EVIDENCE_FILE" \
    --repository "$manifest_repository" --git-sha "$manifest_sha" >/dev/null
  if [[ "$SYNTHETIC_STAGING_BACKUP_BYPASS" == true && "$(production_env_value ALLOW_SYNTHETIC_STAGING_BACKUP_BYPASS "$ENV_FILE")" == true ]]; then
    printf 'staging-only synthetic backup bypass is enabled\n'
  else
    SYNTHETIC_STAGING_BACKUP_BYPASS=false
  fi
fi
[[ -n "$RELEASE_ID" && "$RELEASE_ID" != *"/"* && "$RELEASE_ID" != *".."* ]] || production_die "a safe release ID is required"
API_IMAGE="${API_IMAGE:-$(production_env_value API_IMAGE "$ENV_FILE")}"; WEB_IMAGE="${WEB_IMAGE:-$(production_env_value WEB_IMAGE "$ENV_FILE")}";
CADDY_IMAGE="${CADDY_IMAGE:-$(production_env_value CADDY_IMAGE "$ENV_FILE")}";
production_validate_digest API_IMAGE "$API_IMAGE"
production_validate_digest WEB_IMAGE "$WEB_IMAGE"
production_validate_digest CADDY_IMAGE "$CADDY_IMAGE"
[[ "$API_IMAGE" != *"<"* && "$WEB_IMAGE" != *"<"* && "$CADDY_IMAGE" != *"<"* ]] || production_die "placeholder image values are not deployable"

for pair in \
  "DATABASE_URL_FILE:$(production_env_value DATABASE_URL_FILE "$ENV_FILE")" \
  "JWT_SECRET_KEY_FILE:$(production_env_value JWT_SECRET_KEY_FILE "$ENV_FILE")" \
  "CSRF_SECRET_KEY_FILE:$(production_env_value CSRF_SECRET_KEY_FILE "$ENV_FILE")" \
  "S3_ACCESS_KEY_ID_FILE:$(production_env_value S3_ACCESS_KEY_ID_FILE "$ENV_FILE")" \
  "S3_SECRET_ACCESS_KEY_FILE:$(production_env_value S3_SECRET_ACCESS_KEY_FILE "$ENV_FILE")"; do
  name="${pair%%:*}"; path="${pair#*:}"
  [[ "$(CDPATH='' cd -- "$(dirname -- "$path")" && pwd)" == "$(CDPATH='' cd -- "$SECRET_DIR" && pwd)" ]] || production_die "$name must be inside the configured secret directory"
  production_validate_secret_file "$name" "$path"
done
[[ "$(production_env_value S3_ENDPOINT_URL "$ENV_FILE")" == https://* ]] || production_die "S3_ENDPOINT_URL must use HTTPS"
[[ "$(production_env_value DATABASE_URL_FILE "$ENV_FILE")" != *"/infra/.env"* ]] || production_die "database secret must be outside the checkout"

if [[ "$STAGING_MODE" != true || "$SYNTHETIC_STAGING_BACKUP_BYPASS" != true ]]; then
  [[ -n "$BACKUP_METADATA_FILE" && -f "$BACKUP_METADATA_FILE" ]] || production_die "a recent verified backup metadata file is required"
  backup_age=$(( $(date +%s) - $(stat -c '%Y' "$BACKUP_METADATA_FILE") ))
  (( backup_age >= 0 && backup_age <= BACKUP_MAX_AGE_SECONDS )) || production_die "backup metadata is stale"
  grep -Eq '"database_verified"[[:space:]]*:[[:space:]]*true' "$BACKUP_METADATA_FILE" || production_die "database backup is not verified"
  grep -Eq '"objects_verified"[[:space:]]*:[[:space:]]*true' "$BACKUP_METADATA_FILE" || production_die "object backup is not verified"
fi

available_kb="$(df -Pk "$DEPLOY_DIR" 2>/dev/null | awk 'NR==2 {print $4}')"
[[ "$available_kb" =~ ^[0-9]+$ && "$available_kb" -ge 1048576 ]] || production_die "at least 1 GiB of free disk is required"

mkdir -p "$STATE_DIR"
if [[ "$DEPLOYMENT_ENVIRONMENT" == staging ]]; then
  preflight_args=(--manifest "$RELEASE_MANIFEST_FILE" --evidence "$RELEASE_EVIDENCE_FILE" \
    --env-file "$ENV_FILE" --compose-file "$ROOT_DIR/infra/docker-compose.production.yml" \
    --secret-dir "$SECRET_DIR" --host-root "$DEPLOY_DIR")
  [[ "${STAGING_INITIALIZE_MARKER:-false}" == true ]] && preflight_args+=(--initialize-marker)
  "$ROOT_DIR/scripts/preflight-staging.sh" "${preflight_args[@]}" >/dev/null
fi
exec 9>"$LOCK_FILE"
flock -n 9 || production_die "another deployment is already running"
if [[ "$DRY_RUN" == true ]]; then
  printf 'preflight passed for release %s\n' "$RELEASE_ID"
  exit 0
fi

mkdir -p "$DEPLOY_DIR/releases/$RELEASE_ID/infra/reverse-proxy"
previous_release=""
if [[ -L "$DEPLOY_DIR/current" ]]; then
  previous_release="$(basename "$(readlink "$DEPLOY_DIR/current")")"
fi
cp "$ROOT_DIR/infra/docker-compose.production.yml" "$DEPLOY_DIR/releases/$RELEASE_ID/infra/"
cp "$ROOT_DIR/infra/reverse-proxy/Caddyfile" "$DEPLOY_DIR/releases/$RELEASE_ID/infra/reverse-proxy/"
ln -sfn "$DEPLOY_DIR/releases/$RELEASE_ID" "$DEPLOY_DIR/current"

docker pull "$API_IMAGE" >/dev/null
docker pull "$WEB_IMAGE" >/dev/null
docker pull "$CADDY_IMAGE" >/dev/null
production_validate_digest API_IMAGE "$API_IMAGE"
production_validate_digest WEB_IMAGE "$WEB_IMAGE"
production_validate_digest CADDY_IMAGE "$CADDY_IMAGE"
for image in "$API_IMAGE" "$WEB_IMAGE" "$CADDY_IMAGE"; do
  docker image inspect --format '{{.RepoDigests}}' "$image" | grep -Fq "$image" || production_die "pulled image digest does not match requested digest"
  image_arch="$(docker image inspect --format '{{.Architecture}}' "$image")"
  case "$(uname -m):$image_arch" in
    x86_64:amd64|amd64:amd64|aarch64:arm64|arm64:arm64) ;;
    *) production_die "image architecture is not supported by this host" ;;
  esac
done
[[ -n "$(docker image inspect --format '{{.Config.User}}' "$API_IMAGE")" ]] || production_die "API image must define a non-root user"
[[ -n "$(docker image inspect --format '{{.Config.User}}' "$WEB_IMAGE")" ]] || production_die "web image must define a non-root user"

compose_file="$DEPLOY_DIR/current/infra/docker-compose.production.yml"
project="infinityscan-$RELEASE_ID"
edge_network="infinityscan-edge"
docker network inspect "$edge_network" >/dev/null 2>&1 || docker network create "$edge_network" >/dev/null
slot_override="$DEPLOY_DIR/releases/$RELEASE_ID/compose.slot.yml"
proxy_dir="$DEPLOY_DIR/caddy"
mkdir -p "$proxy_dir"
previous_caddy_file="$proxy_dir/Caddyfile.previous"
had_previous_caddy=false
if [[ -f "$proxy_dir/Caddyfile" ]]; then
  cp "$proxy_dir/Caddyfile" "$previous_caddy_file"
  had_previous_caddy=true
fi
sed "s|{\$CADDY_UPSTREAM}|web-$RELEASE_ID:3000|g" "$DEPLOY_DIR/current/infra/reverse-proxy/Caddyfile" > "$proxy_dir/Caddyfile"
cat > "$slot_override" <<YAML
services:
  caddy:
    volumes:
      - $proxy_dir/Caddyfile:/etc/caddy/Caddyfile:ro
    networks:
      - edge
  web:
    networks:
      edge:
        aliases:
          - web-$RELEASE_ID
      backend: {}
networks:
  edge:
    external: true
    name: $edge_network
YAML
"$ROOT_DIR/scripts/validate-production-config.sh" "$ENV_FILE" "$compose_file" "$SECRET_DIR" >/dev/null
read_revision() {
  production_compose "$ENV_FILE" "$compose_file" "$project" run --rm --entrypoint python migrate -c \
    'from runtime_secrets import required_runtime_value; from sqlalchemy import create_engine,text; from sqlalchemy.pool import NullPool; e=create_engine(required_runtime_value("DATABASE_URL"),poolclass=NullPool); c=e.connect(); print(c.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalar_one_or_none() or "uninitialized"); c.close()'
}

migration_before="$(read_revision 2>/dev/null || printf 'uninitialized')"
production_compose "$ENV_FILE" "$compose_file" "$project" run --rm migrate
migration_after="$(read_revision)"
production_compose "$ENV_FILE" "$compose_file" "$project" up -d api web

wait_healthy() {
  local service="$1" container status
  container="$(production_compose "$ENV_FILE" "$compose_file" "$project" ps -q "$service")"
  for _ in $(seq 1 60); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
    [[ "$status" == healthy ]] && return 0
    sleep 2
  done
  production_die "$service did not become healthy"
}
wait_healthy api
wait_healthy web

proxy_compose=(docker compose --env-file "$ENV_FILE" -f "$compose_file" -f "$slot_override" -p infinityscan-proxy)
"${proxy_compose[@]}" up -d --no-deps caddy >/dev/null
proxy_container="$("${proxy_compose[@]}" ps -q caddy)"
docker exec "$proxy_container" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null

if [[ -n "${DEPLOY_SMOKE_URL:-}" ]]; then
  if ! "$ROOT_DIR/scripts/smoke-deployment.sh" "$DEPLOY_SMOKE_URL"; then
    if [[ "$had_previous_caddy" == true ]]; then
      cp "$previous_caddy_file" "$proxy_dir/Caddyfile"
      docker exec "$proxy_container" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
      production_die "post-switch smoke test failed; Caddy traffic was restored"
    fi
    production_die "first deployment smoke test failed; no previous traffic slot exists"
  fi
fi

python3 - "$STATE_DIR/$RELEASE_ID.json" "$RELEASE_ID" "$API_IMAGE" "$WEB_IMAGE" "$migration_before" "$migration_after" "$previous_release" "$DEPLOYMENT_ENVIRONMENT" "$RELEASE_EVIDENCE_FILE" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
release, api, web, before, after, previous, environment, evidence = sys.argv[2:]
manifest_sha = "unknown"
publication_run_id = None
if evidence:
    evidence_data = json.loads(pathlib.Path(evidence).read_text(encoding="utf-8"))
    manifest_sha = evidence_data["git_sha"]
    publication_run_id = evidence_data["run_id"]
data = {
    "schema_version": 1,
    "environment": environment,
    "release_id": release,
    "git_commit_sha": manifest_sha,
    "api_image_digest": api,
    "web_image_digest": web,
    "deployed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    "migration_revision_before": before,
    "migration_revision_after": after,
    "previous_release_id": previous or None,
    "active_slot": f"slot-{release}",
    "publication_run_id": publication_run_id,
    "smoke_test": "passed" if __import__("os").environ.get("DEPLOY_SMOKE_URL") else "not-run",
    "event": "deploy",
}
path.write_text(json.dumps(data, indent=2) + "\n")
PY
printf 'release %s deployed\n' "$RELEASE_ID"
