#!/usr/bin/env bash
# InfinityScan Phase 1 release-gate audit.
#
# The gate is intentionally strict: required tools, dependency audits, Docker,
# migrations, builds, and health checks are all mandatory.  --skip-docker is
# retained for local diagnostics, but it deliberately fails the gate.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
RESULTS=()
TMPDIR="$(mktemp -d /tmp/infinityscan-phase1-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  RESULTS+=("PASS  $1")
  printf '%bPASS%b  %s\n' "$GREEN" "$NC" "$1"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  RESULTS+=("FAIL  $1")
  printf '%bFAIL%b  %s\n' "$RED" "$NC" "$1"
}

short_error() {
  printf '%s\n' "$1" | tail -n 3 | tr '\n' ' '
}

run_check() {
  local label="$1"
  shift
  local output status
  if output=$("$@" 2>&1); then
    pass "$label"
  else
    status=$?
    fail "$label (exit $status): $(short_error "$output")"
  fi
}

SKIP_DOCKER=false
CLEANUP_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --skip-docker) SKIP_DOCKER=true ;;
    --cleanup) CLEANUP_ONLY=true ;;
    *) fail "Unknown argument: $arg" ;;
  esac
done

ENV_FILE="$REPO_ROOT/infra/.env"
if [ "$CLEANUP_ONLY" = true ]; then
  if [ -f "$ENV_FILE" ]; then
    docker compose --env-file "$ENV_FILE" -f infra/docker-compose.yml down -v --remove-orphans
  else
    docker compose -f infra/docker-compose.yml down -v --remove-orphans
  fi
  exit 0
fi

if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO_ROOT/infra/.env.example" "$ENV_FILE"
fi

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/infra/docker-compose.yml")
env_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted { value=substr($0, index($0, "=") + 1) } END { print value }' "$ENV_FILE"
}

DB_USER="$(env_value POSTGRES_USER)"
DB_NAME="$(env_value POSTGRES_DB)"
DB_PASS="$(env_value POSTGRES_PASSWORD)"
DB_USER="${DB_USER:-infinityscan}"
DB_NAME="${DB_NAME:-infinityscan}"

compose() { "${COMPOSE[@]}" "$@"; }
db_query() {
  local sql="$1"
  local container
  container="$(compose ps -q db)"
  [ -n "$container" ] || return 1
  docker exec -e "PGPASSWORD=$DB_PASS" "$container" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -t -A -v ON_ERROR_STOP=1 -c "$sql"
}

echo "======================================"
echo "  InfinityScan Phase 1 Audit"
echo "======================================"

for tool in python3 node npm curl git jq; do
  if command -v "$tool" >/dev/null 2>&1; then
    pass "TOOL: $tool available"
  else
    fail "TOOL: $tool is required"
  fi
done
if [ "$SKIP_DOCKER" = true ]; then
  fail "TOOL: Docker checks are mandatory; --skip-docker is not a passing mode"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  pass "TOOL: Docker daemon available"
else
  fail "TOOL: Docker daemon is required"
fi

# Use the development requirements for every verifier-side Python operation.
API_VENV="$REPO_ROOT/api/.venv"
if [ ! -x "$API_VENV/bin/python" ]; then
  API_VENV="/tmp/infinityscan-audit-venv"
  if [ ! -x "$API_VENV/bin/python" ]; then
    run_check "PYTHON: create verifier virtualenv" python3 -m venv "$API_VENV"
  fi
fi
API_PYTHON="$API_VENV/bin/python"
if [ -x "$API_PYTHON" ]; then
  if DEV_INSTALL_OUT=$("$API_PYTHON" -m pip install -q -r api/requirements-dev.txt 2>&1); then
    pass "PYTHON: requirements-dev.txt installed"
  else
    status=$?
    fail "PYTHON: requirements-dev.txt installation failed (exit $status): $(short_error "$DEV_INSTALL_OUT")"
  fi
else
  fail "PYTHON: verifier virtualenv is unavailable"
fi

REAL_ENV="$(git ls-files | awk '/\.env$/ && $0 !~ /\.env\.example$/')"
if [ -z "$REAL_ENV" ]; then
  pass "REQ-06: no real .env tracked in git"
else
  fail "REQ-06: real .env tracked: $REAL_ENV"
fi

MANGA_FILES="$(git ls-files | awk '/\.(jpg|jpeg|png|webp|cbz|cbr)$/ { print; count++; if (count == 5) exit }')"
MANGA_DIRS="$(git ls-files | awk '/^[0-9]+\// { print; count++; if (count == 5) exit }')"
if [ -z "$MANGA_FILES" ] && [ -z "$MANGA_DIRS" ]; then
  pass "REQ-07: no bundled manga content tracked"
else
  fail "REQ-07: manga content tracked: $MANGA_FILES $MANGA_DIRS"
fi

if [ -z "$(git ls-files | awk 'tolower($0) ~ /mangus/ { print; exit }')" ]; then
  pass "REQ-08: no legacy standalone reader tracked"
else
  fail "REQ-08: legacy standalone reader is tracked"
fi

GF_IMPORTS="$(git grep -n 'next/font/google' -- web/src web/app web 2>/dev/null || true)"
GF_URLS="$(git grep -n 'fonts\.googleapis' -- web/src web/app web/public 2>/dev/null || true)"
if [ -z "$GF_IMPORTS" ] && [ -z "$GF_URLS" ]; then
  pass "REQ-09: no Google Fonts reference"
else
  fail "REQ-09: Google Fonts reference found"
fi

if [ -x "$API_PYTHON" ]; then
  if IMPORT_OUT=$(cd api && "$API_PYTHON" -c 'from main import app; assert app.title == "InfinityScan API"' 2>&1); then
    pass "REQ-01: API imports successfully"
  else
    status=$?
    fail "REQ-01: API import failed (exit $status): $(short_error "$IMPORT_OUT")"
  fi

  if SLOW_OUT=$(cd api && "$API_PYTHON" - <<'PY' 2>&1
import inspect

from fastapi import Request
from fastapi.routing import APIRoute
from slowapi.middleware import SlowAPIMiddleware
from limiter import limiter
from main import app

assert app.state.limiter is limiter, "app.state.limiter is not the shared limiter"
def iter_routes(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from iter_routes(route.original_router.routes)

routes = {route.path: route for route in iter_routes(app.routes)}
for path in ("/auth/register", "/auth/login"):
    route = routes.get(path)
    assert route is not None, f"missing APIRoute: {path}"
    signature = inspect.signature(route.endpoint)
    assert any(parameter.annotation is Request for parameter in signature.parameters.values()), (
        f"{path} endpoint has no Request parameter: {signature}"
    )
assert any(middleware.cls is SlowAPIMiddleware for middleware in app.user_middleware), (
    "SlowAPIMiddleware is not installed"
)
print("OK")
PY
  ); then
    pass "REQ-02: shared limiter, auth Request signatures, and SlowAPIMiddleware verified"
  else
    status=$?
    fail "REQ-02: SlowAPI route check failed (exit $status): $(short_error "$SLOW_OUT")"
  fi

  if FK_OUT=$(cd api && "$API_PYTHON" - <<'PY' 2>&1
from models import Bookmark, ReadingProgress, User

assert {"id", "username", "hashed_password", "role"} <= {column.name for column in User.__table__.columns}
assert any("users.id" in str(foreign_key) for foreign_key in Bookmark.__table__.foreign_keys)
assert any("users.id" in str(foreign_key) for foreign_key in ReadingProgress.__table__.foreign_keys)
print("OK")
PY
  ); then
    pass "REQ-04: user table and user foreign keys verified"
  else
    status=$?
    fail "REQ-04: ORM foreign-key check failed (exit $status): $(short_error "$FK_OUT")"
  fi

  if UUID_OUT=$(cd api && "$API_PYTHON" - <<'PY' 2>&1
from models import Bookmark, Chapter, Page, ReadingProgress, RefreshSession, Series, User

models = [User, Bookmark, ReadingProgress, Chapter, Series, Page, RefreshSession]
for model in models:
    column = model.__table__.c.id
    assert type(column.type).__name__ == "Uuid", f"{model.__name__}.id is not Uuid"
    assert column.type.as_uuid is True, f"{model.__name__}.id is not as_uuid"
for model, name in [(Bookmark, "user_id"), (Bookmark, "series_id"),
                    (ReadingProgress, "user_id"), (ReadingProgress, "chapter_id"),
                    (Chapter, "series_id"), (RefreshSession, "user_id")]:
    column = model.__table__.c[name]
    assert type(column.type).__name__ == "Uuid" and column.type.as_uuid is True
print("OK")
PY
  ); then
    pass "REQ-05: ORM UUID ID and foreign-key types are consistent"
  else
    status=$?
    fail "REQ-05: UUID consistency check failed (exit $status): $(short_error "$UUID_OUT")"
  fi
else
  fail "REQ-01/02/04/05: API Python is unavailable"
fi

if [ -x "$API_PYTHON" ]; then
  run_check "REQ-10a: pip-audit runtime requirements" "$API_PYTHON" -m pip_audit -r api/requirements.txt
  run_check "REQ-10b: pip-audit development requirements" "$API_PYTHON" -m pip_audit -r api/requirements-dev.txt
else
  fail "REQ-10a/10b: pip-audit cannot run without API Python"
fi
run_check "REQ-10c: shared npm audit policy" \
  "$REPO_ROOT/scripts/lib/npm-audit.sh" \
  "$REPO_ROOT/web" \
  "$REPO_ROOT/scripts/lib/npm-audit-exceptions.json" \
  "$TMPDIR/npm-audit.json"

if grep -q 'CMD \["uvicorn' api/Dockerfile && ! grep -q 'alembic' api/Dockerfile && grep -q '^  migrate:' infra/docker-compose.yml; then
  pass "REQ-13: migrations are separate from the API runtime"
else
  fail "REQ-13: migration separation is invalid"
fi

if [ -x "$API_PYTHON" ]; then
  if REGRESSION_OUT=$(cd api && "$API_PYTHON" - <<'PY' 2>&1
from fastapi.routing import APIRoute

from main import app
from models import Bookmark, Chapter, Page, ReadingProgress, RefreshSession, Series, User

assert any(route.path == "/health" for route in app.routes if isinstance(route, APIRoute))
for model in (User, Bookmark, ReadingProgress, Chapter, Series, Page, RefreshSession):
    assert model.__table__.c.id.type.as_uuid is True
for model, columns in {
    Bookmark: ("user_id", "series_id"),
    ReadingProgress: ("user_id", "chapter_id"),
    Chapter: ("series_id",),
    RefreshSession: ("user_id",),
}.items():
    for name in columns:
        assert model.__table__.c[name].type.as_uuid is True
print("OK")
PY
  ); then
    pass "REQ-17a: foundational API import, /health route, and database ID regression checks"
  else
    status=$?
    fail "REQ-17a: foundational regression failed (exit $status): $(short_error "$REGRESSION_OUT")"
  fi
else
  fail "REQ-17a: foundational regression cannot run without API Python"
fi
if BUILD_WEB_OUT=$(npm --prefix web run build 2>&1); then
  pass "REQ-17b: frontend foundational build succeeds"
else
  status=$?
  fail "REQ-17b: frontend build failed (exit $status): $(short_error "$BUILD_WEB_OUT")"
fi

if [ "$SKIP_DOCKER" = true ]; then
  fail "REQ-DOCKER: Docker checks were explicitly skipped"
else
  if NATIVE_OUT=$(compose build 2>&1); then
    pass "REQ-11: native Docker Compose build succeeds"
  else
    status=$?
    fail "REQ-11: native Docker Compose build failed (exit $status): $(short_error "$NATIVE_OUT")"
  fi

  BUILDER=infinityscan-multiarch
  BUILDER_READY=false
  if EXISTING_BUILDER_OUT=$(docker buildx inspect "$BUILDER" 2>&1); then
    EXISTING_DRIVER="$(printf '%s\n' "$EXISTING_BUILDER_OUT" | awk -F: '/^Driver:/ { gsub(/^[[:space:]]+/, "", $2); print $2; exit }')"
    if [ "$EXISTING_DRIVER" = "docker-container" ]; then
      BUILDER_READY=true
      pass "REQ-12a: existing $BUILDER uses the docker-container driver"
    elif REMOVE_OUT=$(docker buildx rm "$BUILDER" 2>&1); then
      pass "REQ-12a: removed incompatible $BUILDER driver '$EXISTING_DRIVER'"
    else
      status=$?
      fail "REQ-12a: could not remove incompatible $BUILDER (exit $status): $(short_error "$REMOVE_OUT")"
    fi
  else
    pass "REQ-12a: no conflicting $BUILDER builder exists"
  fi

  if BINFMT_OUT=$(docker run --privileged --rm tonistiigi/binfmt --install arm64 2>&1); then
    pass "REQ-12b: registered arm64 binfmt support"
  else
    status=$?
    fail "REQ-12b: arm64 binfmt registration failed (exit $status): $(short_error "$BINFMT_OUT")"
  fi

  if [ "$BUILDER_READY" = true ]; then
    pass "REQ-12c: reusing dedicated $BUILDER builder"
  elif CREATE_OUT=$(docker buildx create --name "$BUILDER" --driver docker-container 2>&1); then
    BUILDER_READY=true
    pass "REQ-12c: created dedicated $BUILDER docker-container builder"
  else
    status=$?
    fail "REQ-12c: builder creation failed (exit $status): $(short_error "$CREATE_OUT")"
  fi

  if INSPECT_OUT=$(docker buildx inspect "$BUILDER" --bootstrap 2>&1); then
    if printf '%s\n' "$INSPECT_OUT" | grep -q 'linux/arm64'; then
      pass "REQ-12d: exact builder supports linux/arm64"
    else
      fail "REQ-12d: exact builder does not advertise linux/arm64"
    fi
  else
    status=$?
    fail "REQ-12d: exact builder inspection failed (exit $status): $(short_error "$INSPECT_OUT")"
  fi

  BUILDX_CACHE_ROOT="$REPO_ROOT/.cache"
  mkdir -p "$BUILDX_CACHE_ROOT"

  run_arm64_build() {
    local label="$1" component="$2" cache_dir="$3"
    shift 3
    local cache_output="${cache_dir}.new" log_file="$TMPDIR/buildx-${component}.log" status
    local cache_from=()
    rm -rf "$cache_output"
    if [ -f "$cache_dir/index.json" ]; then
      cache_from=(--cache-from "type=local,src=$cache_dir")
    fi
    if docker buildx build \
      --builder "$BUILDER" \
      --platform linux/arm64 \
      --load \
      --progress=plain \
      "${cache_from[@]}" \
      --cache-to "type=local,dest=$cache_output,mode=max" \
      "$@" >"$log_file" 2>&1; then
      rm -rf "$cache_dir"
      mv "$cache_output" "$cache_dir"
      pass "$label"
    else
      status=$?
      fail "$label (exit $status): $(tail -n 12 "$log_file" | tr '\n' ' ')"
      rm -rf "$cache_output"
    fi
  }

  run_arm64_build \
    "REQ-12e: API linux/arm64 --load build succeeds" \
    api \
    "$BUILDX_CACHE_ROOT/buildx-api" \
    -f api/Dockerfile \
    -t infinityscan-api:arm64 \
    api

  run_arm64_build \
    "REQ-12f: web linux/arm64 --load build succeeds" \
    web \
    "$BUILDX_CACHE_ROOT/buildx-web" \
    --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 \
    -f web/Dockerfile \
    -t infinityscan-web:arm64 \
    web

  if DOWN_OUT=$(compose down -v --remove-orphans 2>&1); then
    :
  else
    status=$?
    fail "REQ-14: could not reset Docker Compose stack (exit $status): $(short_error "$DOWN_OUT")"
  fi
  if UP_OUT=$(compose up -d db 2>&1); then
    :
  else
    status=$?
    fail "REQ-14: PostgreSQL startup failed (exit $status): $(short_error "$UP_OUT")"
  fi

  DB_READY=false
  for _ in $(seq 1 40); do
    if compose exec -T db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
      DB_READY=true
      break
    fi
    sleep 1
  done
  if [ "$DB_READY" = true ]; then
    pass "REQ-14a: PostgreSQL is healthy"
  else
    fail "REQ-14a: PostgreSQL did not become healthy"
  fi

  if MIGRATE_OUT=$(compose run --rm --build migrate 2>&1); then
    pass "REQ-14b: migration service completed"
    if HEAD_OUT=$(compose run --rm --entrypoint alembic migrate heads 2>&1); then
      EXPECTED_HEAD="$(printf '%s\n' "$HEAD_OUT" | awk '/\(head\)/ { print $1; exit }')"
      REVISION="$(db_query 'SELECT version_num FROM alembic_version;')"
      TABLES="$(db_query "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('users','bookmarks','reading_progress','refresh_sessions','refresh_tokens') ORDER BY table_name;")"
      if [ -z "$EXPECTED_HEAD" ]; then
        fail "REQ-03: could not derive Alembic head from alembic heads"
      elif [ "$REVISION" != "$EXPECTED_HEAD" ]; then
        fail "REQ-03: database revision $REVISION does not equal Alembic head $EXPECTED_HEAD"
      elif ! printf '%s\n' "$TABLES" | grep -qx 'users' || \
           ! printf '%s\n' "$TABLES" | grep -qx 'bookmarks' || \
           ! printf '%s\n' "$TABLES" | grep -qx 'reading_progress' || \
           ! printf '%s\n' "$TABLES" | grep -qx 'refresh_sessions'; then
        fail "REQ-03: required tables are missing: $TABLES"
      elif printf '%s\n' "$TABLES" | grep -qx 'refresh_tokens'; then
        fail "REQ-03: obsolete refresh_tokens table still exists"
      else
        pass "REQ-03: database revision equals dynamic Alembic head $EXPECTED_HEAD and schema is valid"
      fi
    else
      status=$?
      fail "REQ-03: alembic heads failed (exit $status): $(short_error "$HEAD_OUT")"
    fi
  else
    status=$?
    fail "REQ-14b: migration service failed (exit $status): $(short_error "$MIGRATE_OUT")"
  fi

  if STACK_OUT=$(compose up -d api web 2>&1); then
    :
  else
    status=$?
    fail "REQ-15/16: API/web startup failed (exit $status): $(short_error "$STACK_OUT")"
  fi
  API_CODE=000
  WEB_CODE=000
  for _ in $(seq 1 60); do
    API_CODE="$(curl -sS -o /dev/null -w '%{http_code}' http://localhost:8000/health 2>/dev/null || printf '000')"
    WEB_CODE="$(curl -sS -o /dev/null -w '%{http_code}' http://localhost:3000 2>/dev/null || printf '000')"
    if [ "$API_CODE" = 200 ] && [ "$WEB_CODE" = 200 ]; then
      break
    fi
    sleep 2
  done
  if [ "$API_CODE" = 200 ]; then
    pass "REQ-15: API /health returns 200"
  else
    fail "REQ-15: API /health returned $API_CODE"
  fi
  if [ "$WEB_CODE" = 200 ]; then
    pass "REQ-16: frontend returns 200"
  else
    fail "REQ-16: frontend returned $WEB_CODE"
  fi
  compose down -v --remove-orphans >/dev/null
fi

echo ""
echo "======================================"
echo "  Phase 1 Audit Summary"
echo "======================================"
for result in "${RESULTS[@]}"; do
  printf '  %s\n' "$result"
done
printf 'Passed: %d\nFailed: %d\nSkipped: %d\n' "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"

if [ "$FAIL_COUNT" -eq 0 ] && [ "$SKIP_COUNT" -eq 0 ]; then
  printf '%bPhase 1 gate: PASS%b\n' "$GREEN" "$NC"
  exit 0
fi
printf '%bPhase 1 gate: FAIL%b\n' "$RED" "$NC"
exit 1
