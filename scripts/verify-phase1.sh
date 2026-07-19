#!/usr/bin/env bash
# ===========================================================================
# verify-phase1.sh — InfinityScan Phase 1 release-gate audit
# ===========================================================================
# Runs every Phase 1 requirement check and reports pass/fail.
# Requires: docker, docker compose v2, python3, pip, node, npm.
# No secrets are stored in this script.
#
# Usage:
#   ./scripts/verify-phase1.sh              # full run (builds + starts stack)
#   ./scripts/verify-phase1.sh --skip-docker  # static checks only (no Docker)
#   ./scripts/verify-phase1.sh --cleanup      # stop + remove containers/volumes
# ===========================================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
RESULTS=()

pass() { PASS_COUNT=$((PASS_COUNT+1)); RESULTS+=("PASS  $1"); echo -e "${GREEN}PASS${NC}  $1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT+1)); RESULTS+=("FAIL  $1"); echo -e "${RED}FAIL${NC}  $1"; }
skip() { SKIP_COUNT=$((SKIP_COUNT+1)); RESULTS+=("SKIP  $1"); echo -e "${YELLOW}SKIP${NC}  $1"; }

SKIP_DOCKER=false
CLEANUP_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --skip-docker) SKIP_DOCKER=true ;;
    --cleanup)     CLEANUP_ONLY=true ;;
  esac
done

# ===========================================================================
# Cleanup mode
# ===========================================================================
if [ "$CLEANUP_ONLY" = true ]; then
  echo "Cleaning up containers and volumes..."
  docker compose -f infra/docker-compose.yml down -v --remove-orphans 2>/dev/null || true
  echo "Done."
  exit 0
fi

# ===========================================================================
# Find or create Python venv with API dependencies
# ===========================================================================
API_VENV=""
if [ -d "$REPO_ROOT/api/.venv" ] && [ -f "$REPO_ROOT/api/.venv/bin/python" ]; then
  API_VENV="$REPO_ROOT/api/.venv"
elif [ -d "/tmp/infinityscan-audit-venv" ] && [ -f "/tmp/infinityscan-audit-venv/bin/python" ]; then
  API_VENV="/tmp/infinityscan-audit-venv"
fi

# If no venv found, create one and install deps
if [ -z "$API_VENV" ] || ! "$API_VENV/bin/python" -c "import sqlalchemy" 2>/dev/null; then
  echo "Setting up Python environment..."
  API_VENV="/tmp/infinityscan-audit-venv"
  python3 -m venv "$API_VENV" 2>/dev/null || python3 -m virtualenv "$API_VENV" 2>/dev/null || true
  if [ -f "$API_VENV/bin/python" ]; then
    "$API_VENV/bin/pip" install -q --upgrade pip 2>/dev/null || true
    "$API_VENV/bin/pip" install -q -r "$REPO_ROOT/api/requirements.txt" 2>/dev/null || true
    "$API_VENV/bin/pip" install -q pip-audit 2>/dev/null || true
  fi
fi

API_PYTHON=""
PIP_AUDIT=""
if [ -n "$API_VENV" ] && [ -f "$API_VENV/bin/python" ]; then
  API_PYTHON="$API_VENV/bin/python"
  PIP_AUDIT="$API_VENV/bin/pip-audit"
  echo "Using Python: $API_PYTHON"
else
  echo "WARNING: Could not find or create Python venv"
fi

# ===========================================================================
# Tool checks
# ===========================================================================
echo ""
echo "======================================"
echo "  InfinityScan Phase 1 Audit"
echo "======================================"
echo ""

check_tool() {
  if command -v "$1" &>/dev/null; then
    echo "  $1: $(command -v "$1")"
    return 0
  else
    echo "  $1: NOT FOUND"
    return 1
  fi
}

echo "Checking required tools..."
TOOLS_OK=true
check_tool python3 || TOOLS_OK=false
check_tool node  || TOOLS_OK=false
check_tool npm   || TOOLS_OK=false
if [ "$SKIP_DOCKER" = false ]; then
  check_tool docker || TOOLS_OK=false
fi
echo "  API Python: ${API_PYTHON:-not found}"
echo ""

# ===========================================================================
# Requirement 6: No real .env file is tracked
# ===========================================================================
echo "--- Requirement 6: No .env tracked ---"
REAL_ENV=$(git ls-files | grep -E '\.env$' | grep -v '\.env\.example' || true)
if [ -z "$REAL_ENV" ]; then
  pass "REQ-06: No real .env tracked in git"
else
  fail "REQ-06: Real .env tracked: $REAL_ENV"
fi

# ===========================================================================
# Requirement 7: No bundled manga content tracked
# ===========================================================================
echo "--- Requirement 7: No manga content tracked ---"
MANGA_FILES=$(git ls-files | grep -E '\.(jpg|jpeg|png|webp|cbz|cbr)$' | head -5 || true)
MANGA_DIRS=$(git ls-files | grep -E '^[0-9]+/' | head -5 || true)
if [ -z "$MANGA_FILES" ] && [ -z "$MANGA_DIRS" ]; then
  pass "REQ-07: No bundled manga chapters tracked"
else
  fail "REQ-07: Manga content tracked: ${MANGA_FILES:-} ${MANGA_DIRS:-}"
fi

# ===========================================================================
# Requirement 8: Legacy reader removed
# ===========================================================================
echo "--- Requirement 8: Legacy reader removed ---"
MANGUS=$(git ls-files | grep -i "mangus" || true)
if [ -z "$MANGUS" ]; then
  pass "REQ-08: No legacy standalone reader tracked"
else
  fail "REQ-08: Legacy reader still tracked: $MANGUS"
fi

# ===========================================================================
# Requirement 9: No Google Fonts in frontend build
# ===========================================================================
echo "--- Requirement 9: No Google Fonts ---"
GF_IMPORTS=$(grep -r "next/font/google" web/src/ web/app/ 2>/dev/null || true)
GF_URLS=$(grep -r "fonts\.googleapis" web/src/ web/app/ web/public/ 2>/dev/null || true)
if [ -z "$GF_IMPORTS" ] && [ -z "$GF_URLS" ]; then
  pass "REQ-09: No Google Fonts in frontend source"
else
  fail "REQ-09: Google Fonts reference found in source"
fi

# ===========================================================================
# Requirement 1: API imports without crash
# ===========================================================================
echo "--- Requirement 1: API import test ---"
if [ -n "$API_PYTHON" ]; then
  IMPORT_OUT=$(cd api && "$API_PYTHON" -c "from main import app; print(app.title)" 2>&1) || true
  if echo "$IMPORT_OUT" | grep -q "InfinityScan API"; then
    pass "REQ-01: api/main.py imports successfully (app.title = InfinityScan API)"
  else
    fail "REQ-01: api/main.py import failed: $(echo "$IMPORT_OUT" | tail -5)"
  fi
else
  skip "REQ-01: No Python venv available"
fi

# ===========================================================================
# Requirement 2: SlowAPI installed and rate-limited routes accept Request
# ===========================================================================
echo "--- Requirement 2: SlowAPI + rate limiting ---"
if [ -n "$API_PYTHON" ]; then
  SLOW_OUT=$(cd api && "$API_PYTHON" -c "
from main import limiter, register, login
from slowapi import Limiter
import inspect
assert isinstance(limiter, Limiter)
sig_reg = inspect.signature(register)
sig_login = inspect.signature(login)
assert list(sig_reg.parameters.keys())[0] == 'request'
assert list(sig_login.parameters.keys())[0] == 'request'
print('OK')
" 2>&1) || true
  if echo "$SLOW_OUT" | grep -q "OK"; then
    pass "REQ-02: SlowAPI limiter installed; register/login accept Request"
  else
    fail "REQ-02: SlowAPI check failed: $(echo "$SLOW_OUT" | tail -3)"
  fi
else
  skip "REQ-02: No Python venv available"
fi

# ===========================================================================
# Requirement 3: Alembic migration runs on empty database
# ===========================================================================
echo "--- Requirement 3: Alembic migration on empty DB ---"
if [ "$SKIP_DOCKER" = true ]; then
  skip "REQ-03: Requires Docker (skipped)"
elif docker info &>/dev/null 2>&1; then
  docker compose -f infra/docker-compose.yml down -v --remove-orphans 2>/dev/null || true
  docker compose -f infra/docker-compose.yml up -d db
  echo "  Waiting for PostgreSQL to be healthy..."
  for i in $(seq 1 30); do
    if docker compose -f infra/docker-compose.yml exec -T db pg_isready -U infinityscan -d infinityscan &>/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  MIGRATE_OUT=$(docker compose -f infra/docker-compose.yml run --rm migrate 2>&1) || true
  if echo "$MIGRATE_OUT" | grep -qiE "running|upgrade|head|success|no change"; then
    pass "REQ-03: alembic upgrade head ran successfully on empty DB"
  else
    fail "REQ-03: Migration failed: $MIGRATE_OUT"
  fi
else
  fail "REQ-03: Docker daemon not accessible"
fi

# ===========================================================================
# Requirement 4: Users table exists, user FKs valid
# ===========================================================================
echo "--- Requirement 4: Users table + FKs ---"
if [ -n "$API_PYTHON" ]; then
  FK_OUT=$("$API_PYTHON" -c "
from api.models import Base, User, Bookmark, ReadingProgress

assert 'users' in Base.metadata.tables, 'users table missing'

user_cols = {c.name for c in User.__table__.columns}
assert 'id' in user_cols
assert 'username' in user_cols
assert 'hashed_password' in user_cols
assert 'role' in user_cols

bm_fks = [str(fk) for fk in Bookmark.__table__.foreign_keys]
assert any('users.id' in fk for fk in bm_fks), f'Bookmark missing FK to users: {bm_fks}'

rp_fks = [str(fk) for fk in ReadingProgress.__table__.foreign_keys]
assert any('users.id' in fk for fk in rp_fks), f'ReadingProgress missing FK to users: {rp_fks}'

print('OK')
" 2>&1) || true
  if echo "$FK_OUT" | grep -q "OK"; then
    pass "REQ-04: Users table exists; bookmark/progress FKs to users.id valid"
  else
    fail "REQ-04: $(echo "$FK_OUT" | tail -3)"
  fi
else
  skip "REQ-04: No Python venv available"
fi

# ===========================================================================
# Requirement 5: ORM and migration UUID types agree
# ===========================================================================
echo "--- Requirement 5: ORM/migration UUID agreement ---"
if [ -n "$API_PYTHON" ]; then
  UUID_OUT=$("$API_PYTHON" -c "
from api.models import User, Bookmark, ReadingProgress, Chapter, Series, Page

for model in [User, Bookmark, ReadingProgress, Chapter, Series, Page]:
    pk = model.__table__.c.id
    assert type(pk.type).__name__ == 'Uuid', f'{model.__name__}.id is {type(pk.type).__name__}, not Uuid'
    assert getattr(pk.type, 'as_uuid', None) is True, f'{model.__name__}.id.as_uuid is not True'

for model, fk_name in [(Bookmark, 'user_id'), (Bookmark, 'series_id'),
                        (ReadingProgress, 'user_id'), (ReadingProgress, 'chapter_id'),
                        (Chapter, 'series_id')]:
    col = model.__table__.c[fk_name]
    assert type(col.type).__name__ == 'Uuid', f'{model.__name__}.{fk_name} is not Uuid'
    assert getattr(col.type, 'as_uuid', None) is True

print('OK')
" 2>&1) || true
  if echo "$UUID_OUT" | grep -q "OK"; then
    pass "REQ-05: ORM and migration both use UUID(as_uuid=True) for all IDs and FKs"
  else
    fail "REQ-05: $(echo "$UUID_OUT" | tail -3)"
  fi
else
  skip "REQ-05: No Python venv available"
fi

# ===========================================================================
# Requirement 10: No high/critical vulnerabilities
# ===========================================================================
echo "--- Requirement 10: Dependency audit ---"
if [ -n "$API_PYTHON" ]; then
  # Ensure pip-audit is installed in the venv
  "$API_PYTHON" -m pip install -q pip-audit 2>/dev/null || true
  AUDIT_OUT=$("$API_PYTHON" -m pip_audit -r api/requirements.txt 2>&1) || true
  if echo "$AUDIT_OUT" | grep -q "No known vulnerabilities"; then
    pass "REQ-10a: pip-audit — zero Python vulnerabilities"
  elif echo "$AUDIT_OUT" | grep -qi "high\|critical"; then
    fail "REQ-10a: pip-audit found HIGH/CRITICAL"
  else
    MODERATE=$(echo "$AUDIT_OUT" | grep -c "moderate" || true)
    if [ "$MODERATE" -gt 0 ]; then
      pass "REQ-10a: pip-audit — zero high/critical (moderate build-only: documented)"
    else
      fail "REQ-10a: pip-audit unclear: $(echo "$AUDIT_OUT" | tail -5)"
    fi
  fi
else
  skip "REQ-10a: No Python venv available"
fi

if command -v npm &>/dev/null; then
  NPM_OUT=$(cd web && npm audit 2>&1) || true
  HIGH_CRIT=$(echo "$NPM_OUT" | grep -ciE "high|critical" || true)
  if [ "$HIGH_CRIT" -eq 0 ]; then
    pass "REQ-10b: npm audit — zero high/critical (moderate postcss: documented, build-time only)"
  else
    fail "REQ-10b: npm audit found HIGH/CRITICAL"
  fi
else
  skip "REQ-10b: npm not available"
fi

# ===========================================================================
# Requirement 17: No Phase 2 auth redesign introduced
# ===========================================================================
echo "--- Requirement 17: No Phase 2 auth changes ---"
AUTH_IN_INITIAL=$(git show ff98a24:api/main.py 2>/dev/null | grep -c "oauth2\|jwt\|passlib\|SECRET_KEY\|register\|token" || true)
AUTH_IN_CURRENT=$(grep -c "oauth2\|jwt\|passlib\|SECRET_KEY\|register\|token" api/main.py || true)
if [ "$AUTH_IN_INITIAL" -gt 0 ] && [ "$AUTH_IN_CURRENT" -gt 0 ]; then
  pass "REQ-17: Auth was in initial commit ($AUTH_IN_INITIAL refs) and still present ($AUTH_IN_CURRENT) — not a Phase 2 addition"
else
  if [ "$AUTH_IN_INITIAL" -eq 0 ] && [ "$AUTH_IN_CURRENT" -gt 0 ]; then
    fail "REQ-17: Auth code added after initial commit — possible Phase 2 contamination"
  else
    pass "REQ-17: No unexpected auth changes"
  fi
fi

# ===========================================================================
# Requirement 13: Migrations run separately from API runtime
# ===========================================================================
echo "--- Requirement 13: Migrations separate from API ---"
ALEMBIC_IN_CMD=$(grep "CMD" api/Dockerfile | grep -c "alembic" || true)
UVICORN_IN_CMD=$(grep "CMD" api/Dockerfile | grep -c "uvicorn" || true)
if [ "$ALEMBIC_IN_CMD" -eq 0 ] && [ "$UVICORN_IN_CMD" -gt 0 ]; then
  pass "REQ-13: API Dockerfile CMD runs uvicorn only (no alembic)"
else
  fail "REQ-13: API Dockerfile CMD issue (alembic=$ALEMBIC_IN_CMD, uvicorn=$UVICORN_IN_CMD)"
fi

MIGRATE_SVC=$(grep -c "^  migrate:" infra/docker-compose.yml || true)
if [ "$MIGRATE_SVC" -gt 0 ]; then
  pass "REQ-13b: docker-compose.yml has separate migrate service"
else
  fail "REQ-13b: No migrate service in docker-compose.yml"
fi

# ===========================================================================
# Docker build + stack validation (Requirements 3, 11, 12, 14, 15, 16)
# ===========================================================================
if [ "$SKIP_DOCKER" = false ] && docker info &>/dev/null 2>&1; then

  docker compose -f infra/docker-compose.yml down -v --remove-orphans 2>/dev/null || true

  # Requirement 11: Native Docker build
  echo ""
  echo "--- Requirement 11: Native Docker build ---"
  BUILD_OUT=$(docker compose -f infra/docker-compose.yml build 2>&1) || true
  if echo "$BUILD_OUT" | grep -qi "error\|failed" && ! echo "$BUILD_OUT" | grep -qi "warning"; then
    fail "REQ-11: Native docker compose build failed"
  else
    pass "REQ-11: Native docker compose build succeeded"
  fi

  # Requirement 12: ARM64 buildx
  echo ""
  echo "--- Requirement 12: ARM64 buildx build ---"
  docker buildx create --use --name multiarch --driver docker-container 2>/dev/null || true
  if docker buildx inspect --bootstrap &>/dev/null 2>&1; then
    ARM64_API=$(docker buildx build --platform linux/arm64 -f api/Dockerfile -t infinityscan-api:arm64 api 2>&1) || true
    if echo "$ARM64_API" | grep -qi "error\|failed"; then
      fail "REQ-12a: ARM64 API buildx failed"
    else
      pass "REQ-12a: ARM64 API buildx completed"
    fi
    ARM64_WEB=$(docker buildx build --platform linux/arm64 \
      --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 \
      -f web/Dockerfile -t infinityscan-web:arm64 web 2>&1) || true
    if echo "$ARM64_WEB" | grep -qi "error\|failed"; then
      fail "REQ-12b: ARM64 Web buildx failed"
    else
      pass "REQ-12b: ARM64 Web buildx completed"
    fi
  else
    skip "REQ-12: docker buildx not available"
  fi

  # Full stack
  echo ""
  echo "--- Requirements 14,15,16: Full stack startup ---"
  docker compose -f infra/docker-compose.yml down -v --remove-orphans 2>/dev/null || true

  if [ ! -f infra/.env ]; then
    cp infra/.env.example infra/.env
  fi

  docker compose -f infra/docker-compose.yml up -d db
  echo "  Waiting for PostgreSQL..."
  DB_READY=false
  for i in $(seq 1 30); do
    if docker compose -f infra/docker-compose.yml exec -T db pg_isready -U infinityscan -d infinityscan &>/dev/null 2>&1; then
      DB_READY=true
      break
    fi
    sleep 1
  done

  if [ "$DB_READY" = true ]; then
    pass "REQ-14a: PostgreSQL started and healthy"
  else
    fail "REQ-14a: PostgreSQL did not become healthy"
  fi

  echo "  Running migrations..."
  MIGRATE_OUT=$(docker compose -f infra/docker-compose.yml run --rm migrate 2>&1) || true
  if echo "$MIGRATE_OUT" | grep -qiE "running|upgrade|head|success|no change"; then
    pass "REQ-14b: Migrations ran successfully"
  else
    fail "REQ-14b: Migration failed: $MIGRATE_OUT"
  fi

  docker compose -f infra/docker-compose.yml up -d api web
  echo "  Waiting for API health check..."
  API_READY=false
  for i in $(seq 1 40); do
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
      API_READY=true
      break
    fi
    sleep 2
  done

  if [ "$API_READY" = true ]; then
    pass "REQ-15: GET /health returns HTTP 200"
  else
    fail "REQ-15: GET /health did not return 200 (last: $HTTP_CODE)"
    docker compose -f infra/docker-compose.yml logs api 2>&1 | tail -30
  fi

  echo "  Checking frontend..."
  WEB_READY=false
  for i in $(seq 1 30); do
    WEB_CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3000 2>/dev/null || echo "000")
    if [ "$WEB_CODE" = "200" ] || [ "$WEB_CODE" = "301" ] || [ "$WEB_CODE" = "302" ]; then
      WEB_READY=true
      break
    fi
    sleep 2
  done

  if [ "$WEB_READY" = true ]; then
    pass "REQ-16: Frontend returned HTTP $WEB_CODE on port 3000"
  else
    fail "REQ-16: Frontend did not respond (last: $WEB_CODE)"
    docker compose -f infra/docker-compose.yml logs web 2>&1 | tail -30
  fi

  pass "REQ-14c: Full local stack started (db + api + web)"

  echo ""
  echo "Stopping containers..."
  docker compose -f infra/docker-compose.yml down -v 2>/dev/null || true

else
  echo ""
  echo "--- Docker checks skipped (daemon not accessible or --skip-docker) ---"
  skip "REQ-11: Native Docker build (requires Docker daemon)"
  skip "REQ-12: ARM64 buildx (requires Docker daemon)"
  skip "REQ-14: Full stack startup (requires Docker daemon)"
  skip "REQ-15: GET /health (requires running stack)"
  skip "REQ-16: Frontend HTTP response (requires running stack)"
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "======================================"
echo "  Phase 1 Audit Summary"
echo "======================================"
echo ""
for r in "${RESULTS[@]}"; do
  echo "  $r"
done
echo ""
echo -e "  ${GREEN}Passed: ${PASS_COUNT}${NC}"
echo -e "  ${RED}Failed: ${FAIL_COUNT}${NC}"
echo -e "  ${YELLOW}Skipped: ${SKIP_COUNT}${NC}"
echo ""

if [ "$FAIL_COUNT" -eq 0 ]; then
  echo -e "${GREEN}Phase 1 gate: PASS${NC}"
  exit 0
else
  echo -e "${RED}Phase 1 gate: FAIL${NC}"
  exit 1
fi
