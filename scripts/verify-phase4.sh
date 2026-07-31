#!/usr/bin/env bash
# InfinityScan Phase 4 bottomless-reader release gate.
# shellcheck disable=SC2016 # bash -c snippets intentionally expand positional parameters in child shells.
set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT" || exit 1
. "$REPO_ROOT/scripts/lib/verify-runtime.sh"
verify_runtime_init phase4

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
RESULTS=()
FAILED_CHECKS=()
CHECK_INDEX=0
TMPDIR="$VERIFY_TMPDIR"
ENV_FILE="$VERIFY_ENV_FILE"
COMPOSE_OVERRIDE="$TMPDIR/docker-compose.override.yml"
FIXTURE_FILE="$TMPDIR/phase4-fixture.json"
API_METRICS_FILE="$TMPDIR/phase4-api-metrics.json"
E2E_LOG="$TMPDIR/playwright-reader.log"
MEMORY_METRICS_FILE="$TMPDIR/phase4-memory.json"
NETWORK_METRICS_FILE="$TMPDIR/phase4-network.json"

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

failure_tail() {
  local file="$1"
  if [ -s "$file" ]; then
    tail -n 12 "$file"
  else
    printf '%s\n' '(no command output)'
  fi
}

run_check() {
  local label="$1" seconds="$2"
  shift 2
  local status log
  CHECK_INDEX=$((CHECK_INDEX + 1))
  log="$TMPDIR/check-${CHECK_INDEX}.log"
  if declare -F "${1:-}" >/dev/null 2>&1; then
    "$@" >"$log" 2>&1
    status=$?
  else
    timeout --foreground "$seconds" "$@" >"$log" 2>&1
    status=$?
  fi
  if [ "$status" -eq 0 ]; then
    pass "$label"
    return 0
  fi
  if [ "$status" -eq 124 ]; then
    fail "$label (timeout after ${seconds}s): $(failure_tail "$log")"
  else
    fail "$label (exit $status): $(failure_tail "$log")"
  fi
  return 1
}

cleanup_stack() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  timeout --foreground 180 "${VERIFY_COMPOSE[@]}" --profile storage down -v --remove-orphans >/dev/null 2>&1
}

# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
  local status=$?
  trap - EXIT
  verify_runtime_cleanup "$status"
  exit $?
}
trap cleanup EXIT

summary() {
  printf '\n======================================\n'
  printf '  Phase 4 Verification Summary\n'
  printf '======================================\n'
  printf 'Passed: %d\nFailed: %d\nSkipped: %d\n' "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
  if [ -s "$API_METRICS_FILE" ]; then
    printf 'API fixture: %s\n' "$(tr -d '\n' < "$API_METRICS_FILE")"
  fi
  if [ -s "$MEMORY_METRICS_FILE" ]; then
    printf 'Memory measurement: %s\n' "$(tr -d '\n' < "$MEMORY_METRICS_FILE")"
  fi
  if [ -s "$NETWORK_METRICS_FILE" ]; then
    printf 'Network measurement: %s\n' "$(tr -d '\n' < "$NETWORK_METRICS_FILE")"
  fi
  if [ "$FAIL_COUNT" -eq 0 ] && [ "$SKIP_COUNT" -eq 0 ]; then
    printf '%bPhase 4 gate: PASS%b\n' "$GREEN" "$NC"
    return 0
  fi
  if [ "${#FAILED_CHECKS[@]}" -gt 0 ]; then
    printf 'Failed checks:\n'
    printf '  - %s\n' "${FAILED_CHECKS[@]}"
  fi
  printf '%bPhase 4 gate: FAIL%b\n' "$RED" "$NC"
  return 1
}

PHASE_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --phase-only) PHASE_ONLY=true ;;
    *) fail "ARGUMENTS: Phase 4 has no skip mode" ;;
  esac
done
if [ "$FAIL_COUNT" -ne 0 ]; then
  summary
  exit 1
fi

printf '======================================\n'
printf '  InfinityScan Phase 4 Verification\n'
printf '======================================\n'

run_prerequisite() {
  local phase="$1" script="$2" status
  local log="$TMPDIR/phase${phase}.log"
  shift 2
  if timeout --foreground 10800 bash "$script" "$@" >"$log" 2>&1; then
    status=0
  else
    status=$?
  fi
  if [ "$status" -eq 0 ] \
      && grep -q '^Failed: 0$' "$log" \
      && grep -q '^Skipped: 0$' "$log" \
      && grep -q "Phase ${phase} gate: PASS" "$log"; then
    pass "PREREQUISITE-P${phase}: exit 0, zero failures, zero mandatory skips"
    return 0
  fi
  if [ "$status" -eq 124 ]; then
    fail "PREREQUISITE-P${phase}: timed out after 10800s: $(failure_tail "$log")"
  else
    fail "PREREQUISITE-P${phase}: exit $status or non-passing summary: $(failure_tail "$log")"
  fi
  return 1
}

if [ "$PHASE_ONLY" = false ]; then
  for phase in 1 2; do
    if ! run_prerequisite "$phase" "$REPO_ROOT/scripts/verify-phase${phase}.sh"; then
      summary
      exit 1
    fi
  done
  if ! run_prerequisite 3 "$REPO_ROOT/scripts/verify-phase3.sh" --phase-only; then
    summary
    exit 1
  fi
fi

for tool in python3 node npm curl jq git docker timeout; do
  if command -v "$tool" >/dev/null 2>&1; then
    pass "TOOL: $tool available"
  else
    fail "TOOL: $tool is required"
  fi
done
if ! run_check "TOOL: verifier disk and inode preflight" 30 verify_runtime_preflight; then
  summary
  exit 1
fi

BRANCH="$(git branch --show-current 2>/dev/null || true)"
if [ -n "$BRANCH" ] && [ "$BRANCH" != master ]; then
  pass "P4-01: current branch is '$BRANCH', not master"
else
  fail "P4-01: Phase 4 must run on a named non-master branch"
fi

MANGA_FILES="$(git ls-files | awk '$0 !~ /^web\/test-results\// && tolower($0) ~ /\.(jpg|jpeg|png|webp|avif|gif|cbz|cbr|pdf)$/ { print; count++; if (count == 10) exit }')"
REAL_ENV="$(git ls-files | awk '/(^|\/)\.env$/ && $0 !~ /\.env\.example$/ { print }')"
SECRET_FILES="$(git ls-files | awk 'tolower($0) ~ /(^|\/)(id_rsa|id_ed25519)$|\.(pem|key|p12|pfx)$/ { print }')"
SECRET_CONTENT="$(git grep -Il -E -- '-----BEGIN ([A-Z ]+ )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}' 2>/dev/null || true)"
if [ -z "$MANGA_FILES" ] && [ -z "$REAL_ENV" ] && [ -z "$SECRET_FILES" ] && [ -z "$SECRET_CONTENT" ]; then
  pass "P4-02: no manga content, environment files, private keys, or credential signatures tracked"
else
  fail "P4-02: tracked content/secrets detected: $MANGA_FILES $REAL_ENV $SECRET_FILES $SECRET_CONTENT"
fi

set_env_value() {
  verify_runtime_set_env "$1" "$2"
}

env_value() {
  verify_runtime_env_value "$1"
}

set_env_value OBJECT_STORAGE_ENABLED true
set_env_value S3_ENDPOINT_URL http://minio:9000
set_env_value S3_REGION auto
set_env_value S3_BUCKET infinityscan-pages
set_env_value S3_ACCESS_KEY_ID "$(env_value MINIO_ROOT_USER)"
set_env_value S3_SECRET_ACCESS_KEY "$(env_value MINIO_ROOT_PASSWORD)"
set_env_value S3_FORCE_PATH_STYLE true

DB_USER="$(env_value POSTGRES_USER)"
DB_NAME="$(env_value POSTGRES_DB)"
DB_PASS="$(env_value POSTGRES_PASSWORD)"
MINIO_USER="$(env_value MINIO_ROOT_USER)"
MINIO_PASS="$(env_value MINIO_ROOT_PASSWORD)"
DB_USER="${DB_USER:-infinityscan}"
DB_NAME="${DB_NAME:-infinityscan}"
DB_PASS="${DB_PASS:-infinityscan}"
MINIO_USER="${MINIO_USER:-minioadmin}"
MINIO_PASS="${MINIO_PASS:-minioadminsecret}"
set_env_value S3_ACCESS_KEY_ID "$MINIO_USER"
set_env_value S3_SECRET_ACCESS_KEY "$MINIO_PASS"
verify_runtime_refresh_compose

cat > "$COMPOSE_OVERRIDE" <<'YAML'
services:
  db:
    ports:
      - "127.0.0.1::5432"
YAML

COMPOSE=(
  "${VERIFY_COMPOSE[@]}"
  -f "$COMPOSE_OVERRIDE"
  --profile storage
)
VERIFY_COMPOSE=("${COMPOSE[@]}")

INFRA_OK=true
DB_CONTAINER=""
run_check "P4-03a: Docker Compose configuration is valid" 60 "${COMPOSE[@]}" config || INFRA_OK=false
run_check "P4-03b: previous containers and volumes removed" 180 "${COMPOSE[@]}" down -v --remove-orphans || INFRA_OK=false
run_check "P4-03c: clean PostgreSQL and MinIO services started" 600 "${COMPOSE[@]}" up -d --build db minio || INFRA_OK=false

DB_HOST_PORT="$(verify_runtime_port db 5432 || true)"
MINIO_HOST_PORT="$(verify_runtime_port minio 9000 || true)"
run_check "P4-03d: PostgreSQL and MinIO became healthy" 120 bash -c '
  for _ in $(seq 1 60); do
    db_ok=false
    minio_ok=false
    docker compose --project-name "$1" --env-file "$2" -f "$3" -f "$4" --profile storage \
      exec -T db pg_isready -U "$5" -d "$6" >/dev/null 2>&1 && db_ok=true
    curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
      "http://127.0.0.1:$7/minio/health/live" >/dev/null 2>&1 && minio_ok=true
    if [ "$db_ok" = true ] && [ "$minio_ok" = true ]; then exit 0; fi
    sleep 2
  done
  exit 1
' bash "$VERIFY_PROJECT" "$ENV_FILE" "$REPO_ROOT/infra/docker-compose.yml" "$COMPOSE_OVERRIDE" "$DB_USER" "$DB_NAME" "$MINIO_HOST_PORT" || INFRA_OK=false

run_check "P4-03e: private MinIO fixture bucket created" 180 "${COMPOSE[@]}" run --rm minio-setup || INFRA_OK=false
if [ "$INFRA_OK" = true ]; then
  DB_CONTAINER="$("${COMPOSE[@]}" ps -q db 2>/dev/null | tr -d '[:space:]')"
  if [ -z "$DB_CONTAINER" ]; then
    fail "P4-03f: PostgreSQL container ID is unavailable"
    INFRA_OK=false
  fi
fi

MIGRATIONS_OK=false
if [ "$INFRA_OK" = true ]; then
  if run_check "P4-04a: all Alembic migrations completed" 300 "${COMPOSE[@]}" run --rm --build migrate; then
    if run_check "P4-04b: database revision equals the sole Alembic head" 120 bash -c '
      head_revision=$(docker compose --project-name "$1" --env-file "$2" -f "$3" -f "$4" --profile storage run --rm --entrypoint alembic migrate heads | awk "/[(]head[)]/ { print \$1; exit }") || exit $?
      [ -n "$head_revision" ] || exit 1
      actual=$(docker exec -e "PGPASSWORD=$7" "$5" psql -h localhost -U "$6" -d "$8" -t -A -v ON_ERROR_STOP=1 -c "SELECT version_num FROM alembic_version;") || exit $?
      [ "$actual" = "$head_revision" ]
    ' bash "$VERIFY_PROJECT" "$ENV_FILE" "$REPO_ROOT/infra/docker-compose.yml" "$COMPOSE_OVERRIDE" "$DB_CONTAINER" "$DB_USER" "$DB_PASS" "$DB_NAME"; then
      MIGRATIONS_OK=true
    fi
  fi
else
  fail "P4-04: migrations cannot run because clean service setup failed"
fi

API_VENV="$REPO_ROOT/api/.venv"
if [ ! -x "$API_VENV/bin/python" ]; then
  API_VENV=/tmp/infinityscan-audit-venv
fi
API_PYTHON="$API_VENV/bin/python"
if [ ! -x "$API_PYTHON" ]; then
  fail "PYTHON: verifier virtualenv is unavailable after prerequisites"
fi

HOST_DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASS}@127.0.0.1:${DB_HOST_PORT}/${DB_NAME}"
FIXTURE_OK=false
if [ "$MIGRATIONS_OK" = true ] && [ -x "$API_PYTHON" ]; then
  if run_check "P4-05a: 200-chapter reader fixture seeded" 300 env \
      DATABASE_URL="$HOST_DATABASE_URL" \
      OBJECT_STORAGE_ENABLED=true \
      S3_ENDPOINT_URL="http://127.0.0.1:${MINIO_HOST_PORT}" \
      S3_REGION=auto \
      S3_BUCKET=infinityscan-pages \
      S3_ACCESS_KEY_ID="$MINIO_USER" \
      S3_SECRET_ACCESS_KEY="$MINIO_PASS" \
      S3_FORCE_PATH_STYLE=true \
      PYTHONPATH="$REPO_ROOT/api" \
      "$API_PYTHON" api/tools/seed_phase4_reader.py --output "$FIXTURE_FILE"; then
    if run_check "P4-05b: fixture is non-empty with exactly 200 ready chapters and 2000 verified pages" 60 bash -c '
      jq -e ".ready_chapters == 200 and .verified_pages == 2000 and (.first_chapter_id | length > 0)" "$1" >/dev/null || exit $?
      ready=$(docker exec -e "PGPASSWORD=$3" "$2" psql -h localhost -U "$4" -d "$5" -t -A -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM chapters c JOIN series s ON s.id=c.series_id WHERE s.slug=\$\$phase4-reader\$\$ AND c.import_status=\$\$ready\$\$;") || exit $?
      pages=$(docker exec -e "PGPASSWORD=$3" "$2" psql -h localhost -U "$4" -d "$5" -t -A -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pages p JOIN chapters c ON c.id=p.chapter_id JOIN series s ON s.id=c.series_id WHERE s.slug=\$\$phase4-reader\$\$ AND p.integrity_status=\$\$verified\$\$ AND c.import_status=\$\$ready\$\$;") || exit $?
      [ "$ready" = 200 ] && [ "$pages" = 2000 ]
    ' bash "$FIXTURE_FILE" "$DB_CONTAINER" "$DB_PASS" "$DB_USER" "$DB_NAME"; then
      FIXTURE_OK=true
    fi
  fi
else
  fail "P4-05: fixture cannot be seeded because migrations or Python setup failed"
fi

if [ "$INFRA_OK" = true ] && [ -x "$API_PYTHON" ]; then
  TEST_DB=infinityscan_phase4_tests
  if run_check "P4-06a: isolated PostgreSQL reader test database created" 60 bash -c '
      docker exec -e "PGPASSWORD=$2" "$1" psql -h localhost -U "$3" -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $4 WITH (FORCE);" >/dev/null &&
      docker exec -e "PGPASSWORD=$2" "$1" psql -h localhost -U "$3" -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $4;" >/dev/null
    ' bash "$DB_CONTAINER" "$DB_PASS" "$DB_USER" "$TEST_DB"; then
    TEST_DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASS}@127.0.0.1:${DB_HOST_PORT}/${TEST_DB}"
    if run_check "P4-06b: backend cursor reader tests passed on PostgreSQL" 900 env \
        TEST_DATABASE_URL="$TEST_DATABASE_URL" OBJECT_STORAGE_ENABLED=false \
        "$API_PYTHON" -m pytest api/tests/test_reader.py api/tests/test_reader_phase4.py -q; then
      :
    fi
  fi
else
  fail "P4-06: backend reader tests cannot run because PostgreSQL or Python setup failed"
fi

run_check "P4-07: frontend unit tests passed" 900 npm --prefix web run test
run_check "P4-08: ESLint passed" 600 npm --prefix web run lint
run_check "P4-09: TypeScript checking passed" 600 web/node_modules/.bin/tsc --noEmit -p web/tsconfig.json
run_check "P4-10: production frontend build passed" 1200 npm --prefix web run build

STACK_OK=false
if [ "$FIXTURE_OK" = true ]; then
  if run_check "P4-11: complete Docker stack started" 1200 "${COMPOSE[@]}" up -d --build api web; then
    STACK_OK=true
  fi
else
  fail "P4-11: complete stack not started because fixture setup failed"
fi

API_HOST_PORT="$(verify_runtime_port api 8000 || true)"
WEB_HOST_PORT="$(verify_runtime_port web 3000 || true)"
API_BASE="http://127.0.0.1:${API_HOST_PORT}"
WEB_BASE="http://127.0.0.1:${WEB_HOST_PORT}"
ROOT_HTML="$TMPDIR/frontend.html"
if [ "$STACK_OK" = true ]; then
  run_check "P4-12: API health returned HTTP 200 and status ok" 150 bash -c '
    for _ in $(seq 1 75); do
      code=$(curl --silent --show-error --connect-timeout 2 --max-time 5 -o "$2" -w "%{http_code}" "$1/health" 2>/dev/null || true)
      if [ "$code" = 200 ] && jq -e ".status == \"ok\"" "$2" >/dev/null 2>&1; then exit 0; fi
      sleep 2
    done
    exit 1
  ' bash "$API_BASE" "$TMPDIR/api-health.json"
  run_check "P4-13a: frontend health returned HTTP 200" 150 bash -c '
    for _ in $(seq 1 75); do
      code=$(curl --silent --show-error --connect-timeout 2 --max-time 10 -o "$2" -w "%{http_code}" "$1/" 2>/dev/null || true)
      if [ "$code" = 200 ] && [ -s "$2" ]; then exit 0; fi
      sleep 2
    done
    exit 1
  ' bash "$WEB_BASE" "$ROOT_HTML"
  ASSET_PATH="$(grep -oE '/_next/static/[^" ]+\.(js|css)(\?[^" ]*)?' "$ROOT_HTML" 2>/dev/null | sed -n '1p')"
  if [ -n "$ASSET_PATH" ]; then
    run_check "P4-13b: concrete frontend static asset returned HTTP 200 (not 404)" 30 bash -c '
      code=$(curl --silent --show-error --connect-timeout 3 --max-time 15 -o /dev/null -w "%{http_code}" "$1$2") || exit $?
      [ "$code" = 200 ]
    ' bash "$WEB_BASE" "$ASSET_PATH"
  else
    fail "P4-13b: frontend HTML contained no concrete static asset"
  fi
else
  fail "P4-12/P4-13: health and static asset checks cannot run because stack startup failed"
fi

LIVE_API_OK=false
if [ "$STACK_OK" = true ] && [ "$FIXTURE_OK" = true ] && [ -x "$API_PYTHON" ]; then
  if run_check "P4-14 through P4-22: live initial/next/previous cursors, decimal/gap ordering, tamper rejection, ready/verified filtering, safe media paths, and non-404 media passed" 300 \
      "$API_PYTHON" api/tools/verify_phase4_reader.py \
      --api "$API_BASE" --fixture "$FIXTURE_FILE" --output "$API_METRICS_FILE"; then
    LIVE_API_OK=true
  fi
else
  fail "P4-14 through P4-22: live reader contract cannot run because stack or fixture setup failed"
fi

OBSOLETE_HITS="$(git grep -n -E 'all[P]ages|currentChapter[N]um|lastChapter[N]um|parseFloat[(]chapter[N]umber[)]|chapter[N]umber[[:space:]]*[+-][[:space:]]*1|addEventListener[(]["'\'' ]scroll' -- web/src 2>/dev/null || true)"
NUMBER_LINK_HITS="$(git grep -n '/library/' -- web/src 2>/dev/null | grep '/chapter/' || true)"
OLD_PAYLOAD_HITS="$(git grep -n 'Reader[P]ayload' -- api 2>/dev/null || true)"
SCROLL_HITS="$(git grep -n 'scroll[H]eight' -- ':!web/e2e/phase4/reader-long-scroll.spec.ts' ':!web/test-results/**' 2>/dev/null || true)"
if [ -z "$OBSOLETE_HITS" ] && [ -z "$NUMBER_LINK_HITS" ] && [ -z "$OLD_PAYLOAD_HITS" ] && [ -z "$SCROLL_HITS" ]; then
  pass "P4-23a: obsolete reader state, arithmetic, listeners, payload API, media construction, and number links are absent"
else
  fail "P4-23a: obsolete reader matches remain: $OBSOLETE_HITS $NUMBER_LINK_HITS $OLD_PAYLOAD_HITS $SCROLL_HITS"
fi

E2E_OK=false
if [ "$STACK_OK" = true ] && [ "$LIVE_API_OK" = true ] && [ -x web/node_modules/.bin/playwright ]; then
  if timeout --foreground 900 env \
      PLAYWRIGHT_EXTERNAL_SERVER=1 \
      PHASE3_MINIO_HOST_MAP=1 \
      PHASE3_MINIO_PORT="$MINIO_HOST_PORT" \
      API_URL="$API_BASE" \
      WEB_URL="$WEB_BASE" \
      npm --prefix web run test:e2e:phase4 >"$E2E_LOG" 2>&1; then
    pass "P4-23b through P4-34: Playwright 100+ chapter scroll, DOM/data/request bounds, URL/progress/resume/backward/retry/modes, and heap measurement passed"
    E2E_OK=true
  else
    status=$?
    if [ "$status" -eq 124 ]; then
      fail "P4-23b through P4-34: Playwright timed out after 900s: $(failure_tail "$E2E_LOG")"
    else
      fail "P4-23b through P4-34: Playwright failed (exit $status): $(failure_tail "$E2E_LOG")"
    fi
  fi
  if [ "$E2E_OK" = true ]; then
    grep -o '{"phase4MemoryBytes".*}' "$E2E_LOG" | tail -n 1 > "$MEMORY_METRICS_FILE" || true
    grep -o '{"phase4Network".*}' "$E2E_LOG" | tail -n 1 > "$NETWORK_METRICS_FILE" || true
    if [ -s "$MEMORY_METRICS_FILE" ] && [ -s "$NETWORK_METRICS_FILE" ]; then
      pass "P4-34b: heap and request measurements were emitted"
    else
      fail "P4-34b: mandatory heap or request measurement output is missing"
    fi
  fi
else
  fail "P4-23b through P4-34: Playwright reader checks cannot run because setup failed or Playwright is unavailable"
fi

run_check "P4-35: shared npm audit policy passed" 600 \
  "$REPO_ROOT/scripts/lib/npm-audit.sh" \
  "$REPO_ROOT/web" \
  "$REPO_ROOT/scripts/lib/npm-audit-exceptions.json" \
  "$TMPDIR/npm-audit.json"

if [ -x "$API_PYTHON" ]; then
  run_check "P4-36a: pip-audit runtime requirements passed" 600 "$API_PYTHON" -m pip_audit -r api/requirements.txt
  run_check "P4-36b: pip-audit development requirements passed" 600 "$API_PYTHON" -m pip_audit -r api/requirements-dev.txt
else
  fail "P4-36: pip-audit cannot run because API Python is unavailable"
fi

run_check "P4-37a: repository infra/.env remained unchanged" 30 verify_runtime_assert_repo_env_unchanged
if [ "$FAIL_COUNT" -ne 0 ]; then
  verify_runtime_capture_diagnostics
fi
if cleanup_stack; then
  pass "P4-37: containers and volumes removed"
else
  fail "P4-37: container and volume cleanup failed"
fi

if summary; then
  exit 0
fi
exit 1
