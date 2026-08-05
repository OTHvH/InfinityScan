#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
# shellcheck source=scripts/lib/production.sh
source "$ROOT_DIR/scripts/lib/production.sh"

ENV_FILE="${1:-$ROOT_DIR/infra/.env.production}"
COMPOSE_FILE="${2:-$ROOT_DIR/infra/docker-compose.production.yml}"
SECRET_DIR="${3:-}"

production_require_command docker
[[ -f "$ENV_FILE" ]] || production_die "environment file is missing"
[[ -f "$COMPOSE_FILE" ]] || production_die "production Compose file is missing"

tmp_config="$(mktemp)"
trap 'rm -f "$tmp_config"' EXIT
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --format json >"$tmp_config"

SECRET_DIR="${SECRET_DIR:-$(production_env_value DATABASE_URL_FILE "$ENV_FILE" | xargs dirname)}"
[[ -d "$SECRET_DIR" ]] || production_die "secret directory is missing"

python3 - "$tmp_config" "$ENV_FILE" "$SECRET_DIR" <<'PY'
import json
import os
import re
import stat
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
env_file = sys.argv[2]
secret_dir = sys.argv[3]
services = config.get("services", {})
required_services = {"caddy", "migrate", "api", "web"}
if not required_services.issubset(services):
    raise SystemExit("production Compose must contain caddy, migrate, api, and web")
if {"db", "postgres", "minio"} & set(services):
    raise SystemExit("production Compose must not contain PostgreSQL or MinIO")

digest_re = re.compile(r"^ghcr\.io/[^/@\s]+/[^/@\s]+@sha256:[0-9a-fA-F]{64}$")
images = {name: services[name].get("image", "") for name in ("api", "web")}
for name, image in images.items():
    if not digest_re.fullmatch(image):
        raise SystemExit(f"{name} image is not digest-pinned")
if services["migrate"].get("image") != images["api"]:
    raise SystemExit("migration image must equal API image")
if not digest_re.fullmatch(services["caddy"].get("image", "")):
    raise SystemExit("caddy image is not digest-pinned")

for name, service in services.items():
    if service.get("privileged"):
        raise SystemExit(f"{name} must not be privileged")
    if any("docker.sock" in str(volume) for volume in service.get("volumes", [])):
        raise SystemExit(f"{name} must not mount the Docker socket")
    for volume in service.get("volumes", []):
        source = volume.get("source", "") if isinstance(volume, dict) else str(volume).split(":", 1)[0]
        if any(part in str(source).split("/") for part in ("api", "web", "src")):
            raise SystemExit(f"{name} must not mount application source code")
    if not service.get("healthcheck") and name in {"caddy", "api", "web"}:
        raise SystemExit(f"{name} healthcheck is required")
    if not service.get("restart"):
        raise SystemExit(f"{name} restart policy is required")
    logging_options = service.get("logging", {}).get("options", {})
    if not logging_options.get("max-size") or not logging_options.get("max-file"):
        raise SystemExit(f"{name} log rotation is required")
    if not service.get("mem_limit") or not service.get("cpus") or not service.get("pids_limit"):
        raise SystemExit(f"{name} resource limits are required")

ports = services["caddy"].get("ports", [])
published = []
targets = []
for port in ports:
    if isinstance(port, dict):
        published.append(str(port.get("published", "")))
        targets.append(str(port.get("target", "")))
    else:
        fields = str(port).split(":")
        published.append(fields[-2] if len(fields) > 1 else "")
        targets.append(fields[-1])
if set(published) != {"80", "443"} or set(targets) != {"8080", "8443"}:
    raise SystemExit("only Caddy may publish host ports 80 and 443")
for name in ("api", "web", "migrate"):
    if services[name].get("ports"):
        raise SystemExit(f"{name} must not publish host ports")

web_env = services["web"].get("environment", {})
for key in ("DATABASE_URL", "DATABASE_URL_FILE", "JWT_SECRET_KEY", "JWT_SECRET_KEY_FILE", "CSRF_SECRET_KEY", "CSRF_SECRET_KEY_FILE", "S3_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY_FILE"):
    if key in web_env:
        raise SystemExit("backend secrets must not be passed to web")
if web_env.get("NEXT_PUBLIC_API_URL") != "/api":
    raise SystemExit("web must use NEXT_PUBLIC_API_URL=/api")

with open(env_file, encoding="utf-8") as stream:
    env = dict(line.rstrip("\n").split("=", 1) for line in stream if "=" in line and not line.lstrip().startswith("#"))
if env.get("APP_ENV") != "production":
    raise SystemExit("APP_ENV=production is required")
if not env.get("S3_ENDPOINT_URL", "").startswith("https://"):
    raise SystemExit("S3_ENDPOINT_URL must use HTTPS")
for name in ("DATABASE_URL_FILE", "JWT_SECRET_KEY_FILE", "CSRF_SECRET_KEY_FILE", "S3_ACCESS_KEY_ID_FILE", "S3_SECRET_ACCESS_KEY_FILE"):
    path = env.get(name, "")
    if not path or not os.path.isabs(path):
        raise SystemExit(f"{name} must be an absolute external secret path")
    candidate = os.path.join(secret_dir, os.path.basename(path))
    if not os.path.isfile(candidate):
        raise SystemExit(f"required secret file is missing: {name}")
    mode = stat.S_IMODE(os.stat(candidate).st_mode)
    if mode not in (0o400, 0o600):
        raise SystemExit(f"unsafe secret file permissions: {name}")
PY

printf 'production configuration is valid: %s\n' "$COMPOSE_FILE"
