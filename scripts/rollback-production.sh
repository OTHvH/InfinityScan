#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
# shellcheck source=scripts/lib/production.sh
source "$ROOT_DIR/scripts/lib/production.sh"

TARGET="previous"
ENV_FILE="${PRODUCTION_ENV_FILE:-/etc/infinityscan/production.env}"
STATE_DIR="${DEPLOYMENT_STATE_DIR:-/var/lib/infinityscan/releases}"
DEPLOY_DIR="${DEPLOYMENT_DIR:-/opt/infinityscan}"
LOCK_FILE="${DEPLOYMENT_LOCK_FILE:-/var/lib/infinityscan/deploy.lock}"
ENVIRONMENT="${DEPLOYMENT_ENVIRONMENT:-production}"
RELEASE_MANIFEST_FILE="${RELEASE_MANIFEST_FILE:-}"
RELEASE_EVIDENCE_FILE="${RELEASE_EVIDENCE_FILE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --release-manifest) RELEASE_MANIFEST_FILE="$2"; shift 2 ;;
    --release-evidence) RELEASE_EVIDENCE_FILE="$2"; shift 2 ;;
    previous) TARGET=previous; shift ;;
    --*) production_die "unknown rollback option: $1" ;;
    *) TARGET="$1"; shift ;;
  esac
done

if [[ "$ENVIRONMENT" == staging ]]; then
  ENV_FILE="${STAGING_ENV_FILE:-${PRODUCTION_ENV_FILE:-/etc/infinityscan/staging.env}}"
  STATE_DIR="${STAGING_DEPLOYMENT_STATE_DIR:-${DEPLOYMENT_STATE_DIR:-/var/lib/infinityscan/staging/releases}}"
  DEPLOY_DIR="${STAGING_DEPLOYMENT_DIR:-${DEPLOYMENT_DIR:-/opt/infinityscan}}"
  LOCK_FILE="${STAGING_DEPLOYMENT_LOCK_FILE:-${DEPLOYMENT_LOCK_FILE:-/var/lib/infinityscan/staging/deploy.lock}}"
fi

production_require_linux
production_require_command docker
production_require_command python3
[[ -f "$ENV_FILE" ]] || production_die "production environment file is missing"
[[ "$(production_env_value DEPLOYMENT_ENVIRONMENT "$ENV_FILE")" == "$ENVIRONMENT" ]] || production_die "deployment environment does not match the environment file"
[[ -d "$STATE_DIR" ]] || production_die "release state directory is missing"
exec 9>"$LOCK_FILE"
flock -n 9 || production_die "another deployment is already running"

if [[ "$TARGET" == previous ]]; then
  TARGET="$(python3 - "$STATE_DIR" <<'PY'
import json, pathlib, sys
items = []
for path in pathlib.Path(sys.argv[1]).glob("*.json"):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        continue
    if data.get("event") == "deploy" and data.get("release_id"):
        items.append((data.get("deployed_at", ""), data["release_id"]))
if len(items) < 2:
    raise SystemExit("no previous release is available")
items.sort(reverse=True)
print(items[1][1])
PY
)"
fi

metadata="$STATE_DIR/$TARGET.json"
[[ -f "$metadata" ]] || production_die "target release metadata is missing"
api_image="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["api_image_digest"])' "$metadata")"
web_image="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["web_image_digest"])' "$metadata")"
target_revision="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("migration_revision_after", ""))' "$metadata")"
if [[ "$ENVIRONMENT" == staging && -n "$RELEASE_MANIFEST_FILE" && -n "$RELEASE_EVIDENCE_FILE" ]]; then
  manifest_repository="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["repository"])' "$RELEASE_MANIFEST_FILE")"
  manifest_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["git_sha"])' "$RELEASE_MANIFEST_FILE")"
  python3 "$ROOT_DIR/scripts/validate-staging-evidence.py" --manifest "$RELEASE_MANIFEST_FILE" \
    --evidence "$RELEASE_EVIDENCE_FILE" --repository "$manifest_repository" --git-sha "$manifest_sha" >/dev/null
fi
if [[ -n "${CURRENT_MIGRATION_REVISION:-}" && "$CURRENT_MIGRATION_REVISION" != "$target_revision" ]]; then
  production_die "target release does not declare support for the current database revision"
fi
production_validate_digest API_IMAGE "$api_image"
production_validate_digest WEB_IMAGE "$web_image"
caddy_image="$(production_env_value CADDY_IMAGE "$ENV_FILE")"
production_validate_digest CADDY_IMAGE "$caddy_image"

docker image inspect "$api_image" >/dev/null 2>&1 || docker pull "$api_image" >/dev/null
docker image inspect "$web_image" >/dev/null 2>&1 || docker pull "$web_image" >/dev/null
docker image inspect "$caddy_image" >/dev/null 2>&1 || docker pull "$caddy_image" >/dev/null

compose_file="$DEPLOY_DIR/infra/docker-compose.production.yml"
[[ -f "$compose_file" ]] || compose_file="$ROOT_DIR/infra/docker-compose.production.yml"
project="infinityscan-rollback-$TARGET"
edge_network="infinityscan-edge"
docker network inspect "$edge_network" >/dev/null 2>&1 || docker network create "$edge_network" >/dev/null
slot_override="$(mktemp)"
trap 'rm -f "$slot_override"' EXIT
proxy_dir="${DEPLOYMENT_DIR:-/opt/infinityscan}/caddy"
mkdir -p "$proxy_dir"
previous_caddy_file="$proxy_dir/Caddyfile.previous"
had_previous_caddy=false
if [[ -f "$proxy_dir/Caddyfile" ]]; then
  cp "$proxy_dir/Caddyfile" "$previous_caddy_file"
  had_previous_caddy=true
fi
compose_dir="$(dirname "$compose_file")"
sed "s|{\$CADDY_UPSTREAM}|web-$TARGET:3000|g" "$compose_dir/reverse-proxy/Caddyfile" > "$proxy_dir/Caddyfile"
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
          - web-$TARGET
      backend: {}
networks:
  edge:
    external: true
    name: $edge_network
YAML
app_compose=(docker compose --env-file "$ENV_FILE" -f "$compose_file" -f "$slot_override" -p "$project")
"${app_compose[@]}" run --rm migrate >/dev/null
"${app_compose[@]}" up -d api web >/dev/null
proxy_compose=(docker compose --env-file "$ENV_FILE" -f "$compose_file" -f "$slot_override" -p infinityscan-proxy)
"${proxy_compose[@]}" up -d --no-deps caddy >/dev/null
proxy_container="$("${proxy_compose[@]}" ps -q caddy)"
docker exec "$proxy_container" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null

base_url="${ROLLBACK_SMOKE_URL:-}"
if [[ -n "$base_url" ]]; then
  if ! "$ROOT_DIR/scripts/smoke-deployment.sh" "$base_url"; then
    if [[ "$had_previous_caddy" == true ]]; then
      cp "$previous_caddy_file" "$proxy_dir/Caddyfile"
      docker exec "$proxy_container" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
      production_die "rollback smoke test failed; current traffic was restored"
    fi
    production_die "rollback smoke test failed and no prior proxy configuration exists"
  fi
fi

python3 - "$metadata" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["event"] = "rollback"
data["environment"] = data.get("environment", "production")
path.with_name(path.stem + "-rollback.json").write_text(json.dumps(data, indent=2) + "\n")
PY
printf 'rollback prepared for release %s; database was not downgraded\n' "$TARGET"
