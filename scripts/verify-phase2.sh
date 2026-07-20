#!/usr/bin/env bash
# InfinityScan Phase 2 security release-gate verification.
#
# Every check is mandatory.  --skip-docker is retained only as a diagnostic
# switch and fails the gate rather than converting required checks to skips.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
TMPDIR="$(mktemp -d /tmp/infinityscan-phase2-XXXXXX)"
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
RESULTS=()
FAILED_CHECKS=()

pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  RESULTS+=("PASS  $1")
  printf '%bPASS%b  %s\n' "$GREEN" "$NC" "$1"
}

fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  RESULTS+=("FAIL  $1")
  FAILED_CHECKS+=("$1")
  printf '%bFAIL%b  %s\n' "$RED" "$NC" "$1"
}

short_error() {
  printf '%s\n' "$1" | tail -n 8
}

cleanup_stack() {
  if [ -f "$REPO_ROOT/infra/.env" ]; then
    docker compose --env-file "$REPO_ROOT/infra/.env" -f "$REPO_ROOT/infra/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
}

cleanup() {
  local status=$?
  cleanup_stack
  rm -rf "$TMPDIR"
  exit "$status"
}
trap cleanup EXIT

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
if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO_ROOT/infra/.env.example" "$ENV_FILE"
fi
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/infra/docker-compose.yml")
compose() { "${COMPOSE[@]}" "$@"; }
env_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted { value=substr($0, index($0, "=") + 1) } END { print value }' "$ENV_FILE"
}
DB_USER="$(env_value POSTGRES_USER)"
DB_NAME="$(env_value POSTGRES_DB)"
DB_PASS="$(env_value POSTGRES_PASSWORD)"
DB_USER="${DB_USER:-infinityscan}"
DB_NAME="${DB_NAME:-infinityscan}"

if [ "$CLEANUP_ONLY" = true ]; then
  cleanup_stack
  exit 0
fi

API_BASE="http://localhost:8000"
WEB_BASE="http://localhost:3000"
API_VENV="$REPO_ROOT/api/.venv"
if [ ! -x "$API_VENV/bin/python" ]; then
  API_VENV="/tmp/infinityscan-audit-venv"
  if [ ! -x "$API_VENV/bin/python" ]; then
    if ! python3 -m venv "$API_VENV" >/dev/null 2>&1; then
      fail "PYTHON: could not create verifier virtualenv"
    fi
  fi
fi
API_PYTHON="$API_VENV/bin/python"

echo "======================================"
echo "  InfinityScan Phase 2 Verification"
echo "======================================"

for tool in python3 node npm curl jq git; do
  if command -v "$tool" >/dev/null 2>&1; then
    pass "TOOL: $tool available"
  else
    fail "TOOL: $tool is required"
  fi
done

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

run_local_check() {
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

if [ -x "$API_PYTHON" ]; then
  run_local_check "CHECK-A1: backend pytest" "$API_PYTHON" -m pytest api/tests
else
  fail "CHECK-A1: backend pytest cannot run without API Python"
fi
run_local_check "CHECK-A2: frontend unit tests" npm --prefix web run test
run_local_check "CHECK-A3: frontend lint" npm --prefix web run lint
run_local_check "CHECK-A4: TypeScript check" sh -c 'cd web && npx tsc --noEmit'
run_local_check "CHECK-A5: production frontend build" npm --prefix web run build

if [ -x "$API_PYTHON" ]; then
  run_local_check "CHECK-A6: pip-audit runtime dependencies" "$API_PYTHON" -m pip_audit -r api/requirements.txt
  run_local_check "CHECK-A7: pip-audit development dependencies" "$API_PYTHON" -m pip_audit -r api/requirements-dev.txt
else
  fail "CHECK-A6/A7: pip-audit cannot run without API Python"
fi

audit_npm() {
  local report="$TMPDIR/npm-audit.json"
  if (cd web && npm audit --json >"$report" 2>&1); then
    pass "CHECK-A8: npm audit found no vulnerabilities"
    return
  fi
  if node - "$report" <<'NODE'
const fs = require("fs");
const report = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const allowlisted = [];
for (const [name, vulnerability] of Object.entries(report.vulnerabilities ?? {})) {
  const via = vulnerability.via ?? [];
  const postcss = name === "postcss" && via.some(
    (item) => item && typeof item === "object" && item.source === 1117015
  );
  const nextPropagation = name === "next" && via.length === 1 && via[0] === "postcss";
  if (postcss || nextPropagation) {
    allowlisted.push(name);
    continue;
  }
  console.error(`${name}: unallowlisted npm audit finding`);
  process.exitCode = 1;
}
if (process.exitCode !== 1 && allowlisted.length > 0) {
  console.log(`allowlisted PostCSS advisory affecting Next.js: ${allowlisted.join(", ")}`);
}
NODE
  then
    pass "CHECK-A8: npm audit findings are limited to the documented PostCSS/Next.js allowlist"
  else
    fail "CHECK-A8: npm audit found an unallowlisted vulnerability"
  fi
}
audit_npm

if git grep -n -E 'X-User-ID|x_user_id|get_user_id' -- ':!api/tests/**' ':!scripts/**' >/dev/null 2>&1; then
  fail "CHECK-A9: forbidden identity reference exists in runtime or documentation"
else
  pass "CHECK-A9: no forbidden identity reference in runtime or documentation"
fi

if [ "$SKIP_DOCKER" = true ]; then
  fail "CHECK-0: Docker checks were explicitly skipped"
elif docker info >/dev/null 2>&1; then
  pass "CHECK-0: Docker daemon available"
else
  fail "CHECK-0: Docker daemon is required"
fi

STACK_STARTED=false
DB_CONTAINER=""
db_query() {
  local sql="$1"
  [ -n "$DB_CONTAINER" ] || return 1
  docker exec -e "PGPASSWORD=$DB_PASS" "$DB_CONTAINER" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -t -A -v ON_ERROR_STOP=1 -c "$sql"
}

if [ "$SKIP_DOCKER" = false ] && docker info >/dev/null 2>&1; then
  if COMPOSE_CONFIG=$(compose config 2>&1); then
    pass "CHECK-1: docker compose config is valid"
  else
    status=$?
    fail "CHECK-1: docker compose config failed (exit $status): $(short_error "$COMPOSE_CONFIG")"
  fi

  if DOWN_OUT=$(compose down -v --remove-orphans 2>&1); then
    :
  else
    status=$?
    fail "CHECK-2a: could not reset Docker Compose stack (exit $status): $(short_error "$DOWN_OUT")"
  fi
  if START_OUT=$(compose up -d --build 2>&1); then
    STACK_STARTED=true
    pass "CHECK-2: full stack startup command succeeded"
  else
    status=$?
    fail "CHECK-2: full stack startup failed (exit $status): $(short_error "$START_OUT")"
  fi

  DB_READY=false
  for _ in $(seq 1 45); do
    if compose exec -T db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
      DB_READY=true
      break
    fi
    sleep 1
  done
  if [ "$DB_READY" = true ]; then
    pass "CHECK-3: PostgreSQL is healthy"
  else
    fail "CHECK-3: PostgreSQL did not become healthy"
  fi

  DB_CONTAINER="$(compose ps -q db)"
  MIGRATE_CONTAINER="$(compose ps -aq migrate)"
  MIGRATE_EXIT="$(docker inspect "$MIGRATE_CONTAINER" --format '{{.State.ExitCode}}' 2>/dev/null || printf '%s' '-1')"
  if [ "$MIGRATE_EXIT" = 0 ]; then
    pass "CHECK-4: migration container exited 0"
  else
    fail "CHECK-4: migration container exit code is $MIGRATE_EXIT"
  fi

  API_CODE=000
  WEB_CODE=000
  for _ in $(seq 1 60); do
    API_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$API_BASE/health" 2>/dev/null || printf '000')"
    WEB_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$WEB_BASE" 2>/dev/null || printf '000')"
    if [ "$API_CODE" = 200 ] && [ "$WEB_CODE" = 200 ]; then
      break
    fi
    sleep 2
  done
  if [ "$API_CODE" = 200 ]; then
    pass "CHECK-5: API /health returns 200"
  else
    fail "CHECK-5: API /health returned $API_CODE"
  fi
  if [ "$WEB_CODE" = 200 ]; then
    pass "CHECK-6: frontend returns 200"
  else
    fail "CHECK-6: frontend returned $WEB_CODE"
  fi
else
  fail "CHECK-1 through CHECK-6: stack checks cannot run without Docker"
fi

COOKIE_HEADER() {
  local headers_file="$1" cookie_name="$2"
  grep -i "^set-cookie:.*${cookie_name}=" "$headers_file" 2>/dev/null | head -n 1 || true
}
cookie_attr() {
  local headers_file="$1" cookie_name="$2" attr="$3" line lower
  line="$(COOKIE_HEADER "$headers_file" "$cookie_name")"
  lower="$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]')"
  case "$attr" in
    httponly) [[ "$lower" =~ httponly ]] && printf true || printf false ;;
    secure) printf '%s' "$lower" | grep -qE ';[[:space:]]*secure([;[:space:]]|$)' && printf true || printf false ;;
    samesite) printf '%s' "$lower" | sed -n 's/.*samesite=\([^; ]*\).*/\1/p' ;;
    path) printf '%s' "$lower" | sed -n 's/.*path=\([^; ]*\).*/\1/p' ;;
    max-age) printf '%s' "$lower" | sed -n 's/.*max-age=\([^; ]*\).*/\1/p' ;;
    *) printf '' ;;
  esac
}
cookie_value() {
  local jar="$1" name="$2"
  awk -v wanted="$name" '$6 == wanted { print $7; exit }' "$jar" 2>/dev/null || true
}
get_csrf() {
  local jar="$1" response="$2" code token
  if ! code="$(curl -sS -c "$jar" -o "$response" -w '%{http_code}' "$API_BASE/auth/csrf")"; then
    return 1
  fi
  [ "$code" = 200 ] || return 1
  token="$(jq -r '.csrf_token // empty' "$response")"
  [ -n "$token" ] || return 1
  printf '%s' "$token"
}
login_user() {
  local username="$1" password="$2" jar="$3" csrf response code
  response="$TMPDIR/login-$(basename "$jar").json"
  csrf="$(get_csrf "$jar" "$TMPDIR/csrf-$(basename "$jar").json")" || return 1
  if ! code="$(curl -sS -b "$jar" -c "$jar" -o "$response" -w '%{http_code}' \
      -H "X-CSRF-Token: $csrf" -H 'Content-Type: application/json' \
      -d "{\"username\":\"$username\",\"password\":\"$password\"}" \
      "$API_BASE/auth/login")"; then
    return 1
  fi
  [ "$code" = 200 ]
}

REG_USER="verify_user_$(date +%s)"
REG_PASS='VerifyPass123!'
REG_JAR="$TMPDIR/register.jar"
REG_HEADERS="$TMPDIR/register.headers"
REG_BODY="$TMPDIR/register.body"
if REG_TOKEN="$(get_csrf "$REG_JAR" "$TMPDIR/register-csrf.json")"; then
  pass "CHECK-7a: pre-auth CSRF obtained"
else
  REG_TOKEN=''
  fail "CHECK-7a: pre-auth CSRF could not be obtained"
fi
if [ -n "$REG_TOKEN" ] && REG_CODE="$(curl -sS -b "$REG_JAR" -c "$REG_JAR" -D "$REG_HEADERS" -o "$REG_BODY" -w '%{http_code}' \
    -H "X-CSRF-Token: $REG_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$REG_USER\",\"password\":\"$REG_PASS\"}" \
    "$API_BASE/auth/register")"; then
  if [ "$REG_CODE" = 201 ]; then
    pass "CHECK-7b: registration returned 201"
    REG_USER_ID="$(jq -r '.user.id // empty' "$REG_BODY")"
  else
    fail "CHECK-7b: registration returned $REG_CODE"
  fi
else
  REG_CODE=000
  fail "CHECK-7b: registration request failed"
fi

for cookie in is_access is_refresh is_csrf; do
  if [ -n "$(cookie_value "$REG_JAR" "$cookie")" ]; then
    pass "CHECK-7c: registration sets $cookie"
  else
    fail "CHECK-7c: registration missing $cookie"
  fi
done
REG_ME_BODY="$TMPDIR/register-me.body"
if REG_ME_CODE="$(curl -sS -b "$REG_JAR" -o "$REG_ME_BODY" -w '%{http_code}' "$API_BASE/auth/me")" && [ "$REG_ME_CODE" = 200 ] && [ "$(jq -r '.username // empty' "$REG_ME_BODY")" = "$REG_USER" ]; then
  pass "CHECK-7d: /auth/me succeeds immediately after registration"
else
  fail "CHECK-7d: /auth/me did not confirm the registration session"
fi
if jq -e '.access_token // .refresh_token // .token // .access // .refresh' "$REG_BODY" >/dev/null 2>&1 || grep -qE 'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.' "$REG_BODY"; then
  fail "CHECK-7e: registration response contains a token"
else
  pass "CHECK-7e: registration response contains no token"
fi

LOGIN_JAR="$TMPDIR/login.jar"
LOGIN_HEADERS="$TMPDIR/login.headers"
LOGIN_BODY="$TMPDIR/login.body"
if login_user "$REG_USER" "$REG_PASS" "$LOGIN_JAR"; then
  pass "CHECK-8: login succeeds"
else
  fail "CHECK-8: login failed"
fi
if [ -f "$LOGIN_JAR" ]; then
  for cookie in is_access is_refresh is_csrf; do
    if [ -n "$(cookie_value "$LOGIN_JAR" "$cookie")" ]; then
      pass "CHECK-8a: login jar contains $cookie"
    else
      fail "CHECK-8a: login jar missing $cookie"
    fi
  done
fi

NO_AUTH_CODE="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$API_BASE/auth/logout" 2>/dev/null || printf '000')"
if [ "$NO_AUTH_CODE" = 401 ]; then
  pass "CHECK-9a: unauthenticated unsafe request returns 401"
else
  fail "CHECK-9a: unauthenticated unsafe request returned $NO_AUTH_CODE"
fi
NO_CSRF_CODE="$(curl -sS -b "$LOGIN_JAR" -o /dev/null -w '%{http_code}' -X POST "$API_BASE/auth/logout" 2>/dev/null || printf '000')"
if [ "$NO_CSRF_CODE" = 403 ]; then
  pass "CHECK-9b: authenticated unsafe request without CSRF header returns exactly 403"
else
  fail "CHECK-9b: authenticated unsafe request without CSRF header returned $NO_CSRF_CODE"
fi
BAD_CSRF_CODE="$(curl -sS -b "$LOGIN_JAR" -H 'X-CSRF-Token: invalid-token' -o /dev/null -w '%{http_code}' -X POST "$API_BASE/auth/logout" 2>/dev/null || printf '000')"
if [ "$BAD_CSRF_CODE" = 403 ]; then
  pass "CHECK-9c: authenticated unsafe request with invalid CSRF returns exactly 403"
else
  fail "CHECK-9c: invalid CSRF returned $BAD_CSRF_CODE"
fi

ROT_JAR="$TMPDIR/rotation.jar"
if login_user "$REG_USER" "$REG_PASS" "$ROT_JAR"; then
  OLD_REFRESH="$(cookie_value "$ROT_JAR" is_refresh)"
  OLD_ACCESS="$(cookie_value "$ROT_JAR" is_access)"
  OLD_CSRF="$(cookie_value "$ROT_JAR" is_csrf)"
  SESSION_INFO="$(db_query "SELECT id::text || '|' || family_id::text FROM refresh_sessions WHERE user_id = '$REG_USER_ID' ORDER BY created_at DESC LIMIT 1;")"
  OLD_SESSION_ID="${SESSION_INFO%%|*}"
  FAMILY_ID="${SESSION_INFO#*|}"
  REFRESH_CODE="$(curl -sS -b "$ROT_JAR" -c "$ROT_JAR" -H "X-CSRF-Token: $OLD_CSRF" -o /dev/null -w '%{http_code}' -X POST "$API_BASE/auth/refresh")"
  if [ "$REFRESH_CODE" = 200 ]; then
    pass "CHECK-10a: refresh rotation returns 200"
  else
    fail "CHECK-10a: refresh rotation returned $REFRESH_CODE"
  fi
  OLD_REVOKED="$(db_query "SELECT (revoked_at IS NOT NULL)::text FROM refresh_sessions WHERE id = '$OLD_SESSION_ID';")"
  if [ "$OLD_REVOKED" = true ] || [ "$OLD_REVOKED" = t ]; then
    pass "CHECK-10b: rotated old session is revoked in PostgreSQL"
  else
    fail "CHECK-10b: rotated old session is not revoked in PostgreSQL"
  fi
  NEW_ACCESS="$(cookie_value "$ROT_JAR" is_access)"
  NEW_CSRF="$(cookie_value "$ROT_JAR" is_csrf)"
  REUSE_JAR="$TMPDIR/reuse.jar"
  printf '# Netscape HTTP Cookie File\nlocalhost\tFALSE\t/\tFALSE\t0\tis_refresh\t%s\nlocalhost\tFALSE\t/\tFALSE\t0\tis_access\t%s\nlocalhost\tFALSE\t/\tFALSE\t0\tis_csrf\t%s\n' "$OLD_REFRESH" "$NEW_ACCESS" "$NEW_CSRF" > "$REUSE_JAR"
  REUSE_CODE="$(curl -sS -b "$REUSE_JAR" -H "X-CSRF-Token: $NEW_CSRF" -o /dev/null -w '%{http_code}' -X POST "$API_BASE/auth/refresh")"
  if [ "$REUSE_CODE" = 401 ]; then
    pass "CHECK-10c: old refresh token reuse returns exactly 401"
  else
    fail "CHECK-10c: old refresh token reuse returned $REUSE_CODE"
  fi
  ACTIVE_FAMILY="$(db_query "SELECT count(*) FROM refresh_sessions WHERE family_id = '$FAMILY_ID' AND revoked_at IS NULL;")"
  if [ "$ACTIVE_FAMILY" = 0 ]; then
    pass "CHECK-10d: refresh reuse revoked every active session in the family"
  else
    fail "CHECK-10d: refresh family still has $ACTIVE_FAMILY active sessions"
  fi
else
  fail "CHECK-10: could not create a session for refresh verification"
fi

USERA_JAR="$TMPDIR/usera.jar"
USERB_REG_JAR="$TMPDIR/userb-registration.jar"
USERB_JAR="$TMPDIR/userb.jar"
USER_B="verify_user_b_$(date +%s)"
USER_B_PASS='VerifyPass456!'
if B_TOKEN="$(get_csrf "$USERB_REG_JAR" "$TMPDIR/userb-csrf.json")" && B_REG_CODE="$(curl -sS -b "$USERB_REG_JAR" -c "$USERB_REG_JAR" -H "X-CSRF-Token: $B_TOKEN" -H 'Content-Type: application/json' -d "{\"username\":\"$USER_B\",\"password\":\"$USER_B_PASS\"}" -o /dev/null -w '%{http_code}' "$API_BASE/auth/register")" && [ "$B_REG_CODE" = 201 ]; then
  pass "CHECK-11a: User B registration succeeds"
else
  fail "CHECK-11a: User B registration failed"
fi
rm -f "$USERB_JAR"
if login_user "$REG_USER" "$REG_PASS" "$USERA_JAR"; then
  pass "CHECK-11b: User A login succeeds"
else
  fail "CHECK-11b: User A login failed"
fi
if login_user "$USER_B" "$USER_B_PASS" "$USERB_JAR"; then
  pass "CHECK-11c: User B login succeeds"
else
  fail "CHECK-11c: User B login failed"
fi

SERIES_SLUG="verify-series-$(date +%s%N)"
UA_CSRF="$(cookie_value "$USERA_JAR" is_csrf)"
UA_BM_BODY="$TMPDIR/usera-bookmark.json"
UA_BM_CODE="$(curl -sS -b "$USERA_JAR" -H "X-CSRF-Token: $UA_CSRF" -H 'Content-Type: application/json' -d "{\"series_path_word\":\"$SERIES_SLUG\",\"series_name\":\"Verifier Series\"}" -o "$UA_BM_BODY" -w '%{http_code}' -X POST "$API_BASE/bookmarks")"
if [ "$UA_BM_CODE" = 201 ]; then
  pass "CHECK-12a: User A bookmark creation returns documented 201"
else
  fail "CHECK-12a: User A bookmark creation returned $UA_BM_CODE"
fi
UA_BOOKMARKS="$TMPDIR/usera-bookmarks.json"
UB_BOOKMARKS="$TMPDIR/userb-bookmarks.json"
UA_LIST_CODE="$(curl -sS -b "$USERA_JAR" -o "$UA_BOOKMARKS" -w '%{http_code}' "$API_BASE/bookmarks")"
UB_LIST_CODE="$(curl -sS -b "$USERB_JAR" -o "$UB_BOOKMARKS" -w '%{http_code}' "$API_BASE/bookmarks")"
UA_BM_COUNT="$(jq 'length' "$UA_BOOKMARKS")"
UB_BM_COUNT="$(jq 'length' "$UB_BOOKMARKS")"
if [ "$UA_LIST_CODE" = 200 ] && [ "$UA_BM_COUNT" -gt 0 ]; then
  pass "CHECK-12b: User A bookmark count is greater than zero ($UA_BM_COUNT)"
else
  fail "CHECK-12b: User A bookmark list failed or is empty (HTTP $UA_LIST_CODE, count $UA_BM_COUNT)"
fi
if [ "$UB_LIST_CODE" = 200 ] && [ "$UB_BM_COUNT" = 0 ]; then
  pass "CHECK-12c: User B cannot see User A bookmarks"
else
  fail "CHECK-12c: User B bookmark isolation failed (HTTP $UB_LIST_CODE, count $UB_BM_COUNT)"
fi

SERIES_ID="$(db_query "SELECT id::text FROM series WHERE slug = '$SERIES_SLUG';")"
CHAPTER_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
if [ -n "$SERIES_ID" ] && db_query "INSERT INTO chapters (id, series_id, number, language) VALUES ('$CHAPTER_UUID', '$SERIES_ID', 1.0, 'en');" >/dev/null; then
  pass "CHECK-13a: seeded real Series and Chapter in PostgreSQL"
else
  fail "CHECK-13a: could not seed real Series and Chapter"
fi
UA_PROGRESS_CSRF="$(cookie_value "$USERA_JAR" is_csrf)"
UB_PROGRESS_CSRF="$(cookie_value "$USERB_JAR" is_csrf)"
PROGRESS_BODY="$TMPDIR/progress.json"
PROGRESS_CODE="$(curl -sS -b "$USERA_JAR" -H "X-CSRF-Token: $UA_PROGRESS_CSRF" -H 'Content-Type: application/json' -d "{\"chapter_uuid\":\"$CHAPTER_UUID\",\"last_page\":5,\"scroll_position\":0.5,\"completed\":false}" -o "$PROGRESS_BODY" -w '%{http_code}' -X POST "$API_BASE/progress/$SERIES_SLUG/$CHAPTER_UUID")"
if [ "$PROGRESS_CODE" = 200 ]; then
  pass "CHECK-13b: User A saved valid progress"
else
  fail "CHECK-13b: User A progress save returned $PROGRESS_CODE"
fi
UA_PROGRESS_GET="$TMPDIR/usera-progress.json"
UA_PROGRESS_GET_CODE="$(curl -sS -b "$USERA_JAR" -o "$UA_PROGRESS_GET" -w '%{http_code}' "$API_BASE/progress/$SERIES_SLUG/$CHAPTER_UUID")"
if [ "$UA_PROGRESS_GET_CODE" = 200 ] && [ "$(jq -r '.scroll_position' "$UA_PROGRESS_GET")" = 0.5 ]; then
  pass "CHECK-13c: User A reads saved progress successfully"
else
  fail "CHECK-13c: User A could not read saved progress"
fi
UB_PROGRESS_GET="$TMPDIR/userb-progress.json"
UB_PROGRESS_GET_CODE="$(curl -sS -b "$USERB_JAR" -o "$UB_PROGRESS_GET" -w '%{http_code}' "$API_BASE/progress/$SERIES_SLUG/$CHAPTER_UUID")"
if [ "$UB_PROGRESS_GET_CODE" = 200 ] && [ "$(jq -r '.scroll_position' "$UB_PROGRESS_GET")" = null ]; then
  pass "CHECK-13d: User B cannot see User A progress"
else
  fail "CHECK-13d: User B can see User A progress"
fi
UB_PROGRESS_CODE="$(curl -sS -b "$USERB_JAR" -H "X-CSRF-Token: $UB_PROGRESS_CSRF" -H 'Content-Type: application/json' -d "{\"chapter_uuid\":\"$CHAPTER_UUID\",\"last_page\":9,\"scroll_position\":0.9,\"completed\":true}" -o /dev/null -w '%{http_code}' -X POST "$API_BASE/progress/$SERIES_SLUG/$CHAPTER_UUID")"
UA_PROGRESS_AFTER="$TMPDIR/usera-progress-after.json"
curl -sS -b "$USERA_JAR" -o "$UA_PROGRESS_AFTER" "$API_BASE/progress/$SERIES_SLUG/$CHAPTER_UUID" >/dev/null
if [ "$UB_PROGRESS_CODE" = 200 ] && [ "$(jq -r '.scroll_position' "$UA_PROGRESS_AFTER")" = 0.5 ]; then
  pass "CHECK-13e: User B cannot overwrite User A progress"
else
  fail "CHECK-13e: User B progress mutation affected User A"
fi

SERIES_CODE="$(curl -sS -o "$TMPDIR/series.json" -w '%{http_code}' "$API_BASE/series")"
if [ "$SERIES_CODE" = 200 ]; then
  pass "CHECK-14: public GET /series without search query returns 200"
else
  fail "CHECK-14: public GET /series returned $SERIES_CODE"
fi
ROOT_HTML="$TMPDIR/root.html"
ROOT_CODE="$(curl -sS -o "$ROOT_HTML" -w '%{http_code}' "$WEB_BASE")"
ASSET_URL="$(grep -oE '/_next/static/[^" ]+\.(js|css)(\?[^" ]*)?' "$ROOT_HTML" | sed -n '1p')"
if [ -z "$ASSET_URL" ]; then
  fail "CHECK-15a: no concrete /_next/static asset URL found in returned HTML"
else
  ASSET_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$WEB_BASE$ASSET_URL")"
  if [ "$ASSET_CODE" = 200 ]; then
    pass "CHECK-15a: concrete static asset returns 200"
  else
    fail "CHECK-15a: concrete static asset returned $ASSET_CODE"
  fi
fi
if [ "$ROOT_CODE" = 200 ]; then
  pass "CHECK-15b: frontend root returns 200"
else
  fail "CHECK-15b: frontend root returned $ROOT_CODE"
fi

USERA_ME="$TMPDIR/usera-me.json"
SPOOF_CODE="$(curl -sS -b "$USERA_JAR" -H 'X-User-ID: 00000000-0000-0000-0000-000000000000' -o "$USERA_ME" -w '%{http_code}' "$API_BASE/auth/me")"
if [ "$SPOOF_CODE" = 200 ] && [ "$(jq -r '.username' "$USERA_ME")" = "$REG_USER" ]; then
  pass "CHECK-16: identity spoof header cannot change authenticated user"
else
  fail "CHECK-16: identity spoof test returned unexpected identity/status"
fi

if [ -x web/node_modules/.bin/playwright ]; then
  if PLAYWRIGHT_OUT=$(API_URL="$API_BASE" WEB_URL="$WEB_BASE" npm --prefix web run test:e2e -- e2e/auth-flow.spec.ts 2>&1); then
    pass "CHECK-20: Playwright authentication tests"
  else
    status=$?
    fail "CHECK-20: Playwright authentication tests failed (exit $status): $(short_error "$PLAYWRIGHT_OUT")"
  fi
else
  fail "CHECK-20: Playwright is unavailable; authentication tests are mandatory when tooling exists"
fi

RATE_CSRF="$(cookie_value "$USERA_JAR" is_csrf)"
RATE_429=false
RATE_INVALID=false
for i in $(seq 1 40); do
  RATE_CODE="$(curl -sS -b "$USERA_JAR" -H "X-CSRF-Token: $RATE_CSRF" -H 'Content-Type: application/json' -d "{\"series_path_word\":\"rate-series-$i-$(date +%s%N)\",\"series_name\":\"Rate Series\"}" -o /dev/null -w '%{http_code}' -X POST "$API_BASE/bookmarks")"
  if [ "$RATE_CODE" = 429 ]; then
    RATE_429=true
    break
  elif [ "$RATE_CODE" != 201 ]; then
    RATE_INVALID=true
  fi
done
if [ "$RATE_429" = true ] && [ "$RATE_INVALID" = false ]; then
  pass "CHECK-17: valid limited requests reach HTTP 429"
else
  fail "CHECK-17: rate limit did not produce 429 after valid requests"
fi

if PROD_COOKIE_OUT="$(cd api && env APP_ENV=production DATABASE_URL=sqlite:/// CSRF_SECRET_KEY=prod-csrf-secret-012345678901234567890123456789 JWT_SECRET_KEY=prod-jwt-secret-012345678901234567890123456789 COOKIE_SECURE=true COOKIE_SAME_SITE=lax "$API_PYTHON" - <<'PY'
import uuid

from fastapi import Response

from routes.auth import _set_auth_cookies

response = Response()
_set_auth_cookies(response, "access", "refresh", uuid.uuid4())
cookies = response.headers.getlist("set-cookie")
assert len(cookies) == 3, cookies
for name in ("is_access", "is_refresh", "is_csrf"):
    matching = [cookie for cookie in cookies if cookie.startswith(name + "=")]
    assert matching and "; Secure" in matching[0], matching
print("OK")
PY
  )"; then
  pass "CHECK-18: production configuration sets Secure on access, refresh, and CSRF cookies"
else
  fail "CHECK-18: production cookie security check failed"
fi

ORIGIN_CODE="$(curl -sS -b "$USERA_JAR" -H 'Origin: https://evil.example.com' -H 'Content-Type: application/json' -d '{}' -o /dev/null -w '%{http_code}' -X POST "$API_BASE/auth/logout")"
if [ "$ORIGIN_CODE" = 403 ]; then
  pass "CHECK-19: untrusted Origin is rejected with 403"
else
  fail "CHECK-19: untrusted Origin returned $ORIGIN_CODE"
fi

echo ""
echo "======================================"
echo "  Phase 2 Verification Summary"
echo "======================================"
for result in "${RESULTS[@]}"; do
  printf '  %s\n' "$result"
done
printf 'Passed: %d\nFailed: %d\nSkipped: %d\n' "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
if [ "$FAIL_COUNT" -eq 0 ] && [ "$SKIP_COUNT" -eq 0 ]; then
  printf '%bPhase 2 gate: PASS%b\n' "$GREEN" "$NC"
  exit 0
fi
printf '%bPhase 2 gate: FAIL%b\n' "$RED" "$NC"
exit 1
