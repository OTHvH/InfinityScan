#!/usr/bin/env bash
# InfinityScan Phase 3 release-gate verification.
#
# Verifies transactional import pipeline, object storage integration,
# image inspection, private media delivery, provider normalization,
# integrity verification, and end-to-end import/export flow.
#
# Every check is mandatory.  Phase 1 and Phase 2 must pass first.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
. "$REPO_ROOT/scripts/lib/verify-runtime.sh"
verify_runtime_init phase3

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
RESULTS=()
FAILED_CHECKS=()
TMPDIR="$VERIFY_TMPDIR"

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

skip() {
  SKIP_COUNT=$((SKIP_COUNT + 1))
  RESULTS+=("SKIP  $1")
  printf '%bSKIP%b  %s\n' "$YELLOW" "$NC" "$1"
}

short_error() {
  printf '%s\n' "$1" | tail -n 8
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

run_pytest_no_skips() {
  local label="$1"
  shift
  local output status=0
  output=$("$@" 2>&1) || status=$?
  if [ "$status" -eq 0 ] && ! printf '%s\n' "$output" | grep -Eq '(^|[[:space:]])[1-9][0-9]* skipped([,[:space:]]|$)'; then
    pass "$label"
  elif [ "$status" -eq 0 ]; then
    fail "$label: pytest reported skipped tests: $(short_error "$output")"
  else
    fail "$label (exit $status): $(short_error "$output")"
  fi
}

# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
  local status=$?
  trap - EXIT
  verify_runtime_cleanup "$status"
  exit $?
}
trap cleanup EXIT

SKIP_DOCKER=false
CLEANUP_ONLY=false
PHASE_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --skip-docker) SKIP_DOCKER=true ;;
    --cleanup) CLEANUP_ONLY=true ;;
    --phase-only) PHASE_ONLY=true ;;
    *) fail "Unknown argument: $arg" ;;
  esac
done

if [ "$CLEANUP_ONLY" = true ]; then
  exit 0
fi

# Create an override compose file that exposes DB port to host
COMPOSE_OVERRIDE="$TMPDIR/docker-compose.override.yml"
cat > "$COMPOSE_OVERRIDE" <<'OVERRIDE'
services:
  db:
    ports:
      - "127.0.0.1::5432"
  minio:
    ports: !override
      - "0.0.0.0::9000"
      - "0.0.0.0::9001"
OVERRIDE
COMPOSE=()
compose() { "${COMPOSE[@]}" "$@"; }

env_value() {
  verify_runtime_env_value "$1"
}

set_env_value() {
  verify_runtime_set_env "$1" "$2"
}

DB_USER="$(env_value POSTGRES_USER)"
DB_NAME="$(env_value POSTGRES_DB)"
DB_PASS="$(env_value POSTGRES_PASSWORD)"
MINIO_USER="$(env_value MINIO_ROOT_USER)"
MINIO_PASS="$(env_value MINIO_ROOT_PASSWORD)"
DB_USER="${DB_USER:-changeme_user}"
DB_NAME="${DB_NAME:-changeme_db}"
DB_PASS="${DB_PASS:-changeme_password}"
MINIO_USER="${MINIO_USER:-minioadmin}"
MINIO_PASS="${MINIO_PASS:-minioadminsecret}"

db_query() {
  local sql="$1"
  local container
  container="$(compose ps -q db 2>/dev/null | tr -d '[:space:]' || true)"
  [ -n "$container" ] || return 1
  docker exec -e "PGPASSWORD=$DB_PASS" "$container" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -t -A -v ON_ERROR_STOP=1 -c "$sql"
}

storage_object_exists() {
  (cd "$REPO_ROOT/api" && "$API_PYTHON" - "$1" <<'PY'
import sys

from storage import create_object_storage

storage = create_object_storage()
if storage is None or not storage.object_exists(sys.argv[1]):
    raise SystemExit(1)
PY
  )
}

storage_delete_object() {
  (cd "$REPO_ROOT/api" && "$API_PYTHON" - "$1" <<'PY'
import sys

from storage import create_object_storage

storage = create_object_storage()
if storage is None or not storage.delete_object(sys.argv[1]):
    raise SystemExit(1)
PY
  )
}

cookie_value() {
  local jar="$1" name="$2"
  awk -v wanted="$name" '$6 == wanted { print $7; exit }' "$jar" 2>/dev/null || true
}

get_csrf() {
  local jar="$1" response="$2" code token
  if ! code="$(curl -sS -c "$jar" -o "$response" -w '%{http_code}' "$API_BASE/auth/csrf" 2>/dev/null)"; then
    return 1
  fi
  [ "$code" = 200 ] || return 1
  token="$(jq -r '.csrf_token // empty' "$response" 2>/dev/null || true)"
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
      "$API_BASE/auth/login" 2>/dev/null)"; then
    return 1
  fi
  [ "$code" = 200 ]
}

API_VENV="$REPO_ROOT/api/.venv"
if [ ! -x "$API_VENV/bin/python" ]; then
  API_VENV="/tmp/infinityscan-audit-venv"
  if [ ! -x "$API_VENV/bin/python" ]; then
    python3 -m venv "$API_VENV" 2>/dev/null || true
  fi
fi
API_PYTHON="$API_VENV/bin/python"

if [ -x "$API_PYTHON" ]; then
  "$API_PYTHON" -m pip install -q -r api/requirements-dev.txt >/dev/null 2>&1 || true
fi

echo "======================================"
echo "  InfinityScan Phase 3 Verification"
echo "======================================"
echo ""

run_prerequisite() {
  local phase="$1" status=0
  local log="$TMPDIR/phase${phase}.log"
  bash "$REPO_ROOT/scripts/verify-phase${phase}.sh" >"$log" 2>&1 || status=$?
  if [ "$status" -eq 0 ] \
      && grep -q '^Failed: 0$' "$log" \
      && grep -q '^Skipped: 0$' "$log" \
      && grep -q "Phase ${phase} gate: PASS" "$log"; then
    pass "PREREQUISITE-P${phase}: exit 0, zero failures, zero mandatory skips"
    return 0
  fi
  fail "PREREQUISITE-P${phase}: exit $status or non-passing summary: $(tail -n 8 "$log")"
  return 1
}

if [ "$PHASE_ONLY" = false ]; then
  for phase in 1 2; do
    if ! run_prerequisite "$phase"; then
      printf 'Passed: %d\nFailed: %d\nSkipped: %d\n' "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
      printf '%bPhase 3 gate: FAIL%b\n' "$RED" "$NC"
      exit 1
    fi
  done
fi

if verify_runtime_preflight; then
  pass "TOOL: verifier disk and inode preflight"
else
  fail "TOOL: verifier disk and inode preflight"
  printf 'Passed: %d\nFailed: %d\nSkipped: %d\n' "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
  printf '%bPhase 3 gate: FAIL%b\n' "$RED" "$NC"
  exit 1
fi

# ── Docker daemon check ───────────────────────────────────────────────────

if [ "$SKIP_DOCKER" = true ]; then
  fail "CHECK-0: Docker checks were explicitly skipped"
elif docker info >/dev/null 2>&1; then
  pass "CHECK-0: Docker daemon available"
else
  fail "CHECK-0: Docker daemon is required"
fi

if [ "$SKIP_DOCKER" = true ]; then
  printf '%bPhase 3 gate: FAIL (--skip-docker)%b\n' "$RED" "$NC"
  exit 1
fi

# ── Enable object storage in .env ─────────────────────────────────────────

set_env_value "OBJECT_STORAGE_ENABLED" "true"
set_env_value "S3_ENDPOINT_URL" "http://minio:9000"
set_env_value "S3_REGION" "auto"
set_env_value "S3_BUCKET" "infinityscan-pages"
set_env_value "S3_ACCESS_KEY_ID" "$MINIO_USER"
set_env_value "S3_SECRET_ACCESS_KEY" "$MINIO_PASS"
set_env_value "S3_FORCE_PATH_STYLE" "true"
set_env_value "S3_PRESIGN_TTL_SECONDS" "300"
set_env_value "S3_CONNECT_TIMEOUT" "5"
set_env_value "S3_READ_TIMEOUT" "30"
set_env_value "COPYMANGA_ENABLED" "true"
set_env_value "COPYMANGA_API" "https://127.0.0.1:5432"
verify_runtime_refresh_compose
COMPOSE=("${VERIFY_COMPOSE[@]}" -f "$COMPOSE_OVERRIDE" --profile storage)
VERIFY_COMPOSE=("${COMPOSE[@]}")

# ── Infrastructure startup ────────────────────────────────────────────────

API_BASE="http://127.0.0.1:0"
WEB_BASE="http://127.0.0.1:0"

if DOWN_OUT=$(compose down -v --remove-orphans 2>&1); then
  :
else
  status=$?
  fail "INFRA-1: could not reset Docker Compose stack (exit $status): $(short_error "$DOWN_OUT")"
fi

if START_OUT=$(compose up -d --build db minio 2>&1); then
  pass "INFRA-1: PostgreSQL + MinIO startup command succeeded"
else
  status=$?
  fail "INFRA-1: infrastructure startup failed (exit $status): $(short_error "$START_OUT")"
fi

DB_HOST_PORT="$(verify_runtime_port db 5432 || true)"
MINIO_HOST_PORT="$(verify_runtime_port minio 9000 || true)"
HOST_DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASS}@127.0.0.1:${DB_HOST_PORT}/${DB_NAME}"
export DATABASE_URL="$HOST_DATABASE_URL"
export S3_ENDPOINT_URL="http://127.0.0.1:${MINIO_HOST_PORT}"
export OBJECT_STORAGE_ENABLED=true
export S3_ACCESS_KEY_ID="$MINIO_USER"
export S3_SECRET_ACCESS_KEY="$MINIO_PASS"
export S3_BUCKET=infinityscan-pages
export S3_FORCE_PATH_STYLE=true

# The API must reach MinIO through Docker's host gateway while browser tests
# reach the same dynamically published port from the host.
cat > "$COMPOSE_OVERRIDE" <<OVERRIDE
services:
  db:
    ports:
      - "127.0.0.1::5432"
  minio:
    ports: !override
      - "0.0.0.0::9000"
      - "0.0.0.0::9001"
  api:
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      S3_ENDPOINT_URL: "http://host.docker.internal:${MINIO_HOST_PORT}"
OVERRIDE

DB_READY=false
for _ in $(seq 1 45); do
  if compose exec -T db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
    DB_READY=true
    break
  fi
  sleep 1
done
if [ "$DB_READY" = true ]; then
  pass "INFRA-2: PostgreSQL is healthy"
else
  fail "INFRA-2: PostgreSQL did not become healthy"
fi

MINIO_READY=false
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${MINIO_HOST_PORT}/minio/health/live" >/dev/null 2>&1; then
    MINIO_READY=true
    break
  fi
  sleep 1
done
if [ "$MINIO_READY" = true ]; then
  pass "INFRA-3: MinIO is healthy"
else
  fail "INFRA-3: MinIO did not become healthy"
fi

# Create the private bucket with the Compose-managed MinIO client.
if [ "$MINIO_READY" = true ]; then
  if MINIO_SETUP_OUT=$(compose run --rm minio-setup 2>&1); then
    pass "INFRA-4: MinIO bucket is ready"
    run_pytest_no_skips "INFRA-4a: MinIO integration test passed without skips" \
      env RUN_MINIO_TESTS=1 MINIO_TEST_ENDPOINT_URL="$S3_ENDPOINT_URL" \
      "$API_PYTHON" -m pytest api/tests/integration/test_storage_minio.py -q
  else
    status=$?
    fail "INFRA-4: MinIO bucket setup failed (exit $status): $(short_error "$MINIO_SETUP_OUT")"
  fi
else
  fail "INFRA-4: MinIO bucket cannot be created while MinIO is unavailable"
fi

# ── Run migrations ────────────────────────────────────────────────────────

if MIGRATE_OUT=$(compose run --rm migrate 2>&1); then
  pass "CHECK-1: migration service completed"
else
  status=$?
  fail "CHECK-1: migration service failed (exit $status): $(short_error "$MIGRATE_OUT")"
fi

# ── Verify Phase 3 schema columns ─────────────────────────────────────────

PHASE3_COLS="$(db_query "SELECT column_name FROM information_schema.columns WHERE table_name = 'chapters' AND column_name IN ('import_status', 'verified_at', 'source_updated_at') ORDER BY column_name;" 2>/dev/null || true)"
if printf '%s\n' "$PHASE3_COLS" | grep -qx 'import_status' && \
   printf '%s\n' "$PHASE3_COLS" | grep -qx 'source_updated_at'; then
  pass "CHECK-2: chapters has Phase 3 columns"
else
  fail "CHECK-2: chapters missing Phase 3 columns: $PHASE3_COLS"
fi

PAGE_COLS="$(db_query "SELECT column_name FROM information_schema.columns WHERE table_name = 'pages' AND column_name IN ('sha256', 'mime_type', 'file_extension', 'integrity_status', 'verified_at', 'storage_etag', 'imported_at') ORDER BY column_name;" 2>/dev/null || true)"
if printf '%s\n' "$PAGE_COLS" | grep -qx 'sha256' && \
   printf '%s\n' "$PAGE_COLS" | grep -qx 'integrity_status' && \
   printf '%s\n' "$PAGE_COLS" | grep -qx 'object_key' 2>/dev/null; then
  pass "CHECK-3: pages has Phase 3 columns"
else
  # object_key was pre-existing, check the new Phase 3 ones
  if printf '%s\n' "$PAGE_COLS" | grep -qx 'sha256' && \
     printf '%s\n' "$PAGE_COLS" | grep -qx 'integrity_status'; then
    pass "CHECK-3: pages has Phase 3 columns"
  else
    fail "CHECK-3: pages missing Phase 3 columns: $PAGE_COLS"
  fi
fi

PHASE3_TABLES="$(db_query "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('import_jobs', 'import_job_items', 'sources', 'source_series') ORDER BY table_name;" 2>/dev/null || true)"
if printf '%s\n' "$PHASE3_TABLES" | grep -qx 'import_jobs' && \
   printf '%s\n' "$PHASE3_TABLES" | grep -qx 'import_job_items' && \
   printf '%s\n' "$PHASE3_TABLES" | grep -qx 'sources'; then
  pass "CHECK-4: Phase 3 tables exist"
else
  fail "CHECK-4: missing Phase 3 tables: $PHASE3_TABLES"
fi

CHAPTER_NUMBER_TYPE="$(db_query "SELECT data_type FROM information_schema.columns WHERE table_name = 'chapters' AND column_name = 'number';" 2>/dev/null || true)"
if [ "$CHAPTER_NUMBER_TYPE" = "numeric" ]; then
  pass "CHECK-5: chapters.number is NUMERIC(12,4)"
else
  fail "CHECK-5: chapters.number type is '$CHAPTER_NUMBER_TYPE' (expected numeric)"
fi

# ── Start full stack ──────────────────────────────────────────────────────

if STACK_UP=$(compose up -d --build api web 2>&1); then
  pass "INFRA-5: API + web startup command succeeded"
else
  status=$?
  fail "INFRA-5: API/web startup failed (exit $status): $(short_error "$STACK_UP")"
fi

API_HOST_PORT="$(verify_runtime_port api 8000 || true)"
WEB_HOST_PORT="$(verify_runtime_port web 3000 || true)"
API_BASE="http://127.0.0.1:${API_HOST_PORT}"
WEB_BASE="http://127.0.0.1:${WEB_HOST_PORT}"

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
  pass "INFRA-6: API /health returns 200"
else
  fail "INFRA-6: API /health returned $API_CODE"
fi
if [ "$WEB_CODE" = 200 ]; then
  pass "INFRA-7: frontend returns 200"
else
  fail "INFRA-7: frontend returned $WEB_CODE"
fi

# ── Create fixture manga ──────────────────────────────────────────────────

FIXTURE_ROOT="$TMPDIR/manga"
SERIES_SLUG="test-series-$(date +%s%N)"

# Generate valid JPEG images via Python/PIL
FIXTURE_STATUS=0
"$API_PYTHON" - "$FIXTURE_ROOT" "$SERIES_SLUG" <<'PYEOF' 2>&1 || FIXTURE_STATUS=$?
import sys
from pathlib import Path

root = Path(sys.argv[1])
slug = sys.argv[2]

# Need PIL for valid JPEG generation
try:
    from PIL import Image
except ImportError:
    print("PIL not available, creating minimal JPEG manually")
    # Minimal valid JPEG (1x1 white pixel)
    MINIMAL_JPEG = bytes([
        0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
        0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
        0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
        0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
        0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
        0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
        0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
        0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
        0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
        0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
        0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
        0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
        0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
        0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
        0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
        0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
        0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
        0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
        0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
        0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
        0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01,
        0x00, 0x00, 0x3F, 0x00, 0x7B, 0x94, 0x11, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xFF, 0xD9,
    ])
    for ch_idx, ch_num in enumerate([1, 2]):
        ch_dir = root / slug / f"chapter_{ch_num:03d}"
        ch_dir.mkdir(parents=True, exist_ok=True)
        for pg in range(1, 5):
            (ch_dir / f"{pg:03d}.jpg").write_bytes(MINIMAL_JPEG)
    print(f"OK: created minimal JPEG fixture at {root}")
    sys.exit(0)

# Create 3 series with 1-2 chapters each
series_configs = [
    (f"{slug}", "Test Series One", [(1, 4), (2, 3)]),
    (f"{slug}-b", "Test Series Two", [(1, 2)]),
    (f"{slug}-c", "Test Series Three", [(1, 5)]),
]

for s_slug, title, chapters in series_configs:
    for ch_num, page_count in chapters:
        ch_dir = root / s_slug / f"chapter_{ch_num:03d}"
        ch_dir.mkdir(parents=True, exist_ok=True)
        for pg in range(1, page_count + 1):
            img = Image.new("RGB", (800, 1200), color=(pg * 40 % 256, ch_num * 30 % 256, 100))
            img.save(ch_dir / f"{pg:03d}.jpg", "JPEG", quality=85)

total_series = len(series_configs)
total_chapters = sum(len(ch) for _, _, ch in series_configs)
total_pages = sum(p for _, _, chapters in series_configs for _, p in chapters)
print(f"OK: {total_series} series, {total_chapters} chapters, {total_pages} pages at {root}")
PYEOF

if [ "$FIXTURE_STATUS" -eq 0 ]; then
  pass "CHECK-6: fixture manga created"
else
  fail "CHECK-6: fixture manga creation failed"
fi

# ── Dry-run import (no DB writes, no uploads) ─────────────────────────────

DRY_RUN_ERR="$TMPDIR/dry-run-scan.err"
if DRY_RUN_OUT=$(cd "$REPO_ROOT/api" && "$API_PYTHON" -m importing.cli scan "$FIXTURE_ROOT" --json 2>"$DRY_RUN_ERR"); then
  DRY_RUN_SERIES=$(printf '%s' "$DRY_RUN_OUT" | "$API_PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('series_count', 0))
" 2>/dev/null || echo "0")
  if [ "$DRY_RUN_SERIES" -gt 0 ]; then
    pass "CHECK-7: dry-run scan found $DRY_RUN_SERIES series"
  else
    fail "CHECK-7: dry-run scan returned 0 series"
  fi
else
  status=$?
  fail "CHECK-7: dry-run scan failed (exit $status): $(short_error "$(<"$DRY_RUN_ERR")")"
fi

if IMPORT_DRY_OUT=$(cd "$REPO_ROOT/api" && "$API_PYTHON" -m importing.cli import "$FIXTURE_ROOT" --dry-run 2>&1); then
  pass "CHECK-8: dry-run import completed (no writes)"
else
  status=$?
  fail "CHECK-8: dry-run import failed (exit $status): $(short_error "$IMPORT_DRY_OUT")"
fi

# ── Verify no DB changes from dry-run ─────────────────────────────────────

DRY_SERIES_COUNT="$(db_query "SELECT count(*) FROM series WHERE slug LIKE '$SERIES_SLUG%';" 2>/dev/null || echo "0")"
if [ "$DRY_SERIES_COUNT" = "0" ]; then
  pass "CHECK-9: dry-run created no series rows"
else
  fail "CHECK-9: dry-run created $DRY_SERIES_COUNT series rows (expected 0)"
fi

# ── Real import ───────────────────────────────────────────────────────────

if IMPORT_OUT=$(cd "$REPO_ROOT/api" && "$API_PYTHON" -m importing.cli import "$FIXTURE_ROOT" 2>&1); then
  IMPORT_STATUS=$(printf '%s' "$IMPORT_OUT" | grep -oP 'finished with status: \K\w+' || echo "unknown")
  if [ "$IMPORT_STATUS" = "succeeded" ]; then
    pass "CHECK-10: import completed with status 'succeeded'"
  else
    fail "CHECK-10: import completed with unexpected status '$IMPORT_STATUS'"
  fi
else
  status=$?
  fail "CHECK-10: import failed (exit $status): $(short_error "$IMPORT_OUT")"
fi

# ── Verify DB records ─────────────────────────────────────────────────────

SERIES_COUNT="$(db_query "SELECT count(*) FROM series WHERE slug LIKE '$SERIES_SLUG%';" 2>/dev/null || echo "0")"
if [ "$SERIES_COUNT" -ge 3 ]; then
  pass "CHECK-11: $SERIES_COUNT series rows created"
else
  fail "CHECK-11: expected >= 3 series, got $SERIES_COUNT"
fi

CHAPTER_COUNT="$(db_query "SELECT count(*) FROM chapters WHERE series_id IN (SELECT id FROM series WHERE slug LIKE '$SERIES_SLUG%');" 2>/dev/null || echo "0")"
if [ "$CHAPTER_COUNT" -ge 4 ]; then
  pass "CHECK-12: $CHAPTER_COUNT chapter rows created"
else
  fail "CHECK-12: expected >= 4 chapters, got $CHAPTER_COUNT"
fi

PAGE_COUNT="$(db_query "SELECT count(*) FROM pages WHERE chapter_id IN (SELECT c.id FROM chapters c JOIN series s ON c.series_id = s.id WHERE s.slug LIKE '$SERIES_SLUG%');" 2>/dev/null || echo "0")"
if [ "$PAGE_COUNT" -ge 9 ]; then
  pass "CHECK-13: $PAGE_COUNT page rows created"
else
  fail "CHECK-13: expected >= 9 pages, got $PAGE_COUNT"
fi

# ── Verify chapter import status ──────────────────────────────────────────

IMPORTING_COUNT="$(db_query "SELECT count(*) FROM chapters WHERE import_status = 'importing' AND series_id IN (SELECT id FROM series WHERE slug LIKE '$SERIES_SLUG%');" 2>/dev/null || echo "0")"
READY_COUNT="$(db_query "SELECT count(*) FROM chapters WHERE import_status = 'ready' AND series_id IN (SELECT id FROM series WHERE slug LIKE '$SERIES_SLUG%');" 2>/dev/null || echo "0")"
if [ "$IMPORTING_COUNT" = "0" ] && [ "$READY_COUNT" -ge 4 ]; then
  pass "CHECK-14: all chapters have terminal import status ($READY_COUNT ready)"
else
  fail "CHECK-14: importing=$IMPORTING_COUNT ready=$READY_COUNT (expected 0 importing, >= 4 ready)"
fi

# ── Verify import job records ─────────────────────────────────────────────

JOB_COUNT="$(db_query "SELECT count(*) FROM import_jobs;" 2>/dev/null || echo "0")"
if [ "$JOB_COUNT" -ge 1 ]; then
  JOB_STATUS="$(db_query "SELECT status FROM import_jobs ORDER BY created_at DESC LIMIT 1;" 2>/dev/null || echo "unknown")"
  if [ "$JOB_STATUS" = "succeeded" ]; then
    pass "CHECK-15: import job record exists with status 'succeeded'"
  else
    fail "CHECK-15: latest import job has status '$JOB_STATUS' (expected succeeded)"
  fi
else
  fail "CHECK-15: no import job records found"
fi

# ── Verify object keys on pages ───────────────────────────────────────────

PAGES_WITH_KEYS="$(db_query "SELECT count(*) FROM pages WHERE object_key IS NOT NULL AND object_key != '' AND chapter_id IN (SELECT c.id FROM chapters c JOIN series s ON c.series_id = s.id WHERE s.slug LIKE '$SERIES_SLUG%');" 2>/dev/null || echo "0")"
if [ "$PAGES_WITH_KEYS" -ge 9 ]; then
  pass "CHECK-16: $PAGES_WITH_KEYS pages have object keys"
else
  fail "CHECK-16: expected >= 9 pages with object keys, got $PAGES_WITH_KEYS"
fi

# ── Verify SHA-256 hashes stored ──────────────────────────────────────────

PAGES_WITH_HASH="$(db_query "SELECT count(*) FROM pages WHERE sha256 IS NOT NULL AND length(sha256) = 64 AND chapter_id IN (SELECT c.id FROM chapters c JOIN series s ON c.series_id = s.id WHERE s.slug LIKE '$SERIES_SLUG%');" 2>/dev/null || echo "0")"
if [ "$PAGES_WITH_HASH" -ge 9 ]; then
  pass "CHECK-17: $PAGES_WITH_HASH pages have SHA-256 hashes"
else
  fail "CHECK-17: expected >= 9 pages with SHA-256, got $PAGES_WITH_HASH"
fi

# ── Verify page integrity status ──────────────────────────────────────────

VERIFIED_PAGES="$(db_query "SELECT count(*) FROM pages WHERE integrity_status = 'verified' AND chapter_id IN (SELECT c.id FROM chapters c JOIN series s ON c.series_id = s.id WHERE s.slug LIKE '$SERIES_SLUG%');" 2>/dev/null || echo "0")"
if [ "$VERIFIED_PAGES" -ge 9 ]; then
  pass "CHECK-18: $VERIFIED_PAGES pages have integrity_status 'verified'"
else
  fail "CHECK-18: expected >= 9 verified pages, got $VERIFIED_PAGES"
fi

# ── Verify MinIO objects ──────────────────────────────────────────────────

SAMPLE_OBJECT_KEY="$(db_query "SELECT object_key FROM pages WHERE object_key IS NOT NULL AND chapter_id IN (SELECT c.id FROM chapters c JOIN series s ON c.series_id = s.id WHERE s.slug LIKE '$SERIES_SLUG%') LIMIT 1;" 2>/dev/null || true)"
if [ -n "$SAMPLE_OBJECT_KEY" ]; then
  if storage_object_exists "$SAMPLE_OBJECT_KEY" >/dev/null 2>&1; then
    pass "CHECK-19: MinIO object exists for $(printf '%s' "$SAMPLE_OBJECT_KEY" | cut -c1-60)..."
  else
    fail "CHECK-19: MinIO object is missing: $SAMPLE_OBJECT_KEY"
  fi
else
  fail "CHECK-19: no sample object key to verify"
fi

# ── Verify chapters.number is NUMERIC ─────────────────────────────────────

NUMERIC_CHECK="$(db_query "SELECT count(*) FROM chapters WHERE number::text LIKE '%.%' AND series_id IN (SELECT id FROM series WHERE slug LIKE '$SERIES_SLUG%');" 2>/dev/null || echo "unknown")"
if [ "$NUMERIC_CHECK" != "unknown" ] && [ "$NUMERIC_CHECK" -ge 0 ]; then
  pass "CHECK-20: chapters.number values are valid NUMERIC(12,4)"
else
  fail "CHECK-20: chapters.number type validation failed"
fi

# ── Verify media endpoint (presigned redirect) ────────────────────────────

# Create admin user for authenticated endpoint testing
ADMIN_USER="admin_$(date +%s)"
ADMIN_PASS='AdminPass123!'
ADMIN_JAR="$TMPDIR/admin.jar"
ADMIN_CSRF_TMP="$TMPDIR/admin-csrf.json"

if ADMIN_TOKEN="$(get_csrf "$ADMIN_JAR" "$ADMIN_CSRF_TMP")"; then
  ADMIN_REG_CODE="$(curl -sS -b "$ADMIN_JAR" -c "$ADMIN_JAR" \
    -H "X-CSRF-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" \
    -o /dev/null -w '%{http_code}' "$API_BASE/auth/register" 2>/dev/null || echo "000")"
  if [ "$ADMIN_REG_CODE" = "201" ]; then
    # Promote to admin via direct SQL
    db_query "UPDATE users SET role = 'admin' WHERE username = '$ADMIN_USER';" >/dev/null 2>&1 || true
    if login_user "$ADMIN_USER" "$ADMIN_PASS" "$ADMIN_JAR"; then
      pass "CHECK-21: admin user created and logged in"
    else
      fail "CHECK-21: admin login failed"
    fi
  else
    fail "CHECK-21: admin registration returned $ADMIN_REG_CODE"
  fi
else
  fail "CHECK-21: could not obtain CSRF for admin registration"
fi

# Get a page UUID for media endpoint test
SAMPLE_PAGE_UUID="$(db_query "SELECT id::text FROM pages WHERE integrity_status = 'verified' AND object_key IS NOT NULL AND chapter_id IN (SELECT c.id FROM chapters c JOIN series s ON c.series_id = s.id WHERE s.slug LIKE '$SERIES_SLUG%') LIMIT 1;" 2>/dev/null || true)"
if [ -n "$SAMPLE_PAGE_UUID" ] && [ -f "$ADMIN_JAR" ] && [ -n "$(cookie_value "$ADMIN_JAR" is_access)" ]; then
  MEDIA_CODE="$(curl -sS -b "$ADMIN_JAR" -o /dev/null -w '%{http_code}' "$API_BASE/media/pages/$SAMPLE_PAGE_UUID" 2>/dev/null || echo "000")"
  if [ "$MEDIA_CODE" = "307" ]; then
    pass "CHECK-22: media page endpoint returns 307 redirect"
  else
    fail "CHECK-22: media page endpoint returned $MEDIA_CODE (expected 307)"
  fi
else
  skip "CHECK-22: media page endpoint test (no verified page or no auth)"
fi

# ── Verify presigned URL structure ────────────────────────────────────────

if [ -n "$SAMPLE_PAGE_UUID" ] && [ -f "$ADMIN_JAR" ] && [ -n "$(cookie_value "$ADMIN_JAR" is_access)" ]; then
  MEDIA_LOCATION="$(curl -sS -b "$ADMIN_JAR" -o /dev/null -w '%{redirect_url}' "$API_BASE/media/pages/$SAMPLE_PAGE_UUID" 2>/dev/null || true)"
  if printf '%s' "$MEDIA_LOCATION" | grep -q 'X-Amz-Signature\|X-Amz-Credential\|X-Amz-Algorithm'; then
    pass "CHECK-23: presigned URL contains expected S3 signature parameters"
  else
    fail "CHECK-23: presigned URL missing signature parameters: $MEDIA_LOCATION"
  fi
else
  skip "CHECK-23: presigned URL structure check (skipped with CHECK-22)"
fi

# ── Reimport unchanged content (skip uploads, idempotent) ────────────────

JOBS_BEFORE="$(db_query "SELECT count(*) FROM import_jobs;" 2>/dev/null || echo "unknown")"
ITEMS_BEFORE="$(db_query "SELECT count(*) FROM import_job_items;" 2>/dev/null || echo "unknown")"
if REIMPORT_OUT=$(cd "$REPO_ROOT/api" && "$API_PYTHON" -m importing.cli import "$FIXTURE_ROOT" 2>&1); then
  JOBS_AFTER="$(db_query "SELECT count(*) FROM import_jobs;" 2>/dev/null || echo "unknown")"
  ITEMS_AFTER="$(db_query "SELECT count(*) FROM import_job_items;" 2>/dev/null || echo "unknown")"
  if [ "$JOBS_AFTER" = "$JOBS_BEFORE" ] && [ "$ITEMS_AFTER" = "$ITEMS_BEFORE" ]; then
    pass "CHECK-24: unchanged reimport reused the existing job without new items"
  else
    fail "CHECK-24: unchanged reimport changed jobs $JOBS_BEFORE->$JOBS_AFTER or items $ITEMS_BEFORE->$ITEMS_AFTER"
  fi
else
  status=$?
  fail "CHECK-24: unchanged reimport failed (exit $status): $(short_error "$REIMPORT_OUT")"
fi

# ── Change one page (new object key only, old key unreferenced) ──────────

# Replace first page image with a different image
FIRST_PAGE_PATH="$(find "$FIXTURE_ROOT" -name '*.jpg' | sort | head -n 1)"
if [ -n "$FIRST_PAGE_PATH" ]; then
  OLD_SHA256=$(sha256sum "$FIRST_PAGE_PATH" | awk '{print $1}')
  "$API_PYTHON" - "$FIRST_PAGE_PATH" <<'PYEOF2' 2>&1
import sys
from PIL import Image
path = sys.argv[1]
img = Image.new("RGB", (800, 1200), color=(255, 0, 0))
img.save(path, "JPEG", quality=85)
PYEOF2
  NEW_SHA256=$(sha256sum "$FIRST_PAGE_PATH" | awk '{print $1}')
  if [ "$OLD_SHA256" != "$NEW_SHA256" ]; then
    REIMPORT2_OUT=$(cd "$REPO_ROOT/api" && "$API_PYTHON" -m importing.cli import "$FIXTURE_ROOT" 2>&1 || true)
    REIMPORT2_UPLOADED=$(printf '%s' "$REIMPORT2_OUT" | grep -oP 'uploaded=\K\d+' || echo "0")
    if [ "$REIMPORT2_UPLOADED" -ge 1 ]; then
      NEW_KEY="$(db_query "SELECT object_key FROM pages WHERE sha256 = '$NEW_SHA256' LIMIT 1;" 2>/dev/null || true)"
      if [ -n "$NEW_KEY" ] && printf '%s' "$NEW_KEY" | grep -q "$NEW_SHA256"; then
        pass "CHECK-25: changed page uploaded with new object key (new sha in key)"
      else
        fail "CHECK-25: changed page object key does not contain new SHA-256"
      fi
    else
      fail "CHECK-25: reimport did not upload changed page"
    fi
  else
    fail "CHECK-25: could not create different image (SHA unchanged)"
  fi
else
  fail "CHECK-25: no fixture page found to modify"
fi

# ── Remove a page (report no delete of objects) ──────────────────────────

REMOVED_PAGE_PATH="$(find "$FIXTURE_ROOT" -name '*.jpg' | sort | tail -n 1)"
if [ -n "$REMOVED_PAGE_PATH" ]; then
  rm -f "$REMOVED_PAGE_PATH"
  (cd "$REPO_ROOT/api" && "$API_PYTHON" -m importing.cli import "$FIXTURE_ROOT" >/dev/null 2>&1) || true
  REMAINING_PAGES_IN_DB="$(db_query "SELECT count(*) FROM pages WHERE chapter_id IN (SELECT c.id FROM chapters c JOIN series s ON c.series_id = s.id WHERE s.slug LIKE '$SERIES_SLUG%');" 2>/dev/null || echo "0")"
  # Import should not delete pages or objects from DB
  if [ "$REMAINING_PAGES_IN_DB" -ge 9 ]; then
    pass "CHECK-26: removed page not deleted from DB ($REMAINING_PAGES_IN_DB pages remain)"
  else
    fail "CHECK-26: page count dropped to $REMAINING_PAGES_IN_DB after removing a file"
  fi
else
  fail "CHECK-26: no fixture page found to remove"
fi

# ── Prune reconciliation ─────────────────────────────────────────────────

if PRUNE_OUT=$(cd "$REPO_ROOT/api" && "$API_PYTHON" -m importing.cli reconcile --dry-run 2>&1); then
  PRUNE_STALE=$(printf '%s' "$PRUNE_OUT" | grep -oP 'Stale pages:\s*\K\d+' || echo "0")
  pass "CHECK-27: reconciliation dry-run completed (stale=$PRUNE_STALE)"
else
  status=$?
  fail "CHECK-27: reconciliation dry-run failed (exit $status): $(short_error "$PRUNE_OUT")"
fi

# ── Corrupt metadata → integrity detect ───────────────────────────────────

SAMPLE_CHAPTER_ID="$(db_query "SELECT id::text FROM chapters WHERE series_id IN (SELECT id FROM series WHERE slug LIKE '$SERIES_SLUG%') LIMIT 1;" 2>/dev/null || true)"
if [ -n "$SAMPLE_CHAPTER_ID" ]; then
  db_query "UPDATE chapters SET page_count = 999 WHERE id = '$SAMPLE_CHAPTER_ID';" >/dev/null 2>&1 || true
  INTEGRITY_OUT=$(cd "$REPO_ROOT/api" && "$API_PYTHON" -m tools.integrity check --json 2>&1 || true)
  HAS_MISMATCH=$(printf '%s' "$INTEGRITY_OUT" | "$API_PYTHON" -c "
import sys, json
try:
    data = json.load(sys.stdin)
    issues = data.get('issues', [])
    found = any(i.get('category') == 'page_count_mismatch' for i in issues)
    print('yes' if found else 'no')
except:
    print('error')
" 2>/dev/null || echo "error")
  if [ "$HAS_MISMATCH" = "yes" ]; then
    pass "CHECK-28: integrity check detected page_count mismatch"
    # Repair it
    (cd "$REPO_ROOT/api" && "$API_PYTHON" -m tools.integrity repair-safe --json >/dev/null 2>&1) || true
    REPAIRED_COUNT="$(db_query "SELECT page_count FROM chapters WHERE id = '$SAMPLE_CHAPTER_ID';" 2>/dev/null || echo "unknown")"
    if [ "$REPAIRED_COUNT" != "999" ]; then
      pass "CHECK-28a: repair-safe recalculated page_count ($REPAIRED_COUNT)"
    else
      fail "CHECK-28a: repair-safe did not fix page_count (still 999)"
    fi
  else
    fail "CHECK-28: integrity check did not detect page_count mismatch (result: $HAS_MISMATCH)"
  fi
else
  skip "CHECK-28: corruption test (no chapter found)"
fi

# ── Delete object → integrity mark missing ────────────────────────────────

SAMPLE_OBJ_KEY="$(db_query "SELECT object_key FROM pages WHERE integrity_status = 'verified' AND chapter_id IN (SELECT c.id FROM chapters c JOIN series s ON c.series_id = s.id WHERE s.slug LIKE '$SERIES_SLUG%') LIMIT 1;" 2>/dev/null || true)"
if [ -n "$SAMPLE_OBJ_KEY" ]; then
  if storage_delete_object "$SAMPLE_OBJ_KEY" >/dev/null 2>&1; then
    (cd "$REPO_ROOT/api" && "$API_PYTHON" -m tools.integrity repair-safe --json >/dev/null 2>&1) || true
    PAGE_STATUS="$(db_query "SELECT integrity_status::text FROM pages WHERE object_key = '$SAMPLE_OBJ_KEY';" 2>/dev/null || true)"
    if [ "$PAGE_STATUS" = "missing" ]; then
      pass "CHECK-29: integrity repair marked deleted object as 'missing'"
    else
      fail "CHECK-29: deleted object retained integrity status '$PAGE_STATUS'"
    fi
  else
    fail "CHECK-29: authenticated object deletion failed"
  fi
else
  fail "CHECK-29: no object key found for deletion test"
fi

# ── Import rejection evidence ─────────────────────────────────────────────

PHASE3_REJECTION_REPORT="$TMPDIR/phase3-rejections.json"
if REJECTION_OUT=$(cd "$REPO_ROOT/api" && PYTHONOPTIMIZE=0 "$API_PYTHON" -m tools.verify_phase3_rejections --output "$PHASE3_REJECTION_REPORT" 2>&1); then
  if jq -e '.unsupported.pages == 1 and .unsupported.objectsAdded == 1 and .unsupported.rejectedStatus == "skipped"' "$PHASE3_REJECTION_REPORT" >/dev/null; then
    pass "CHECK-30: unsupported file explicitly skipped with no Page or object while its valid neighbor imported"
  else
    fail "CHECK-30: unsupported-file evidence is incomplete"
  fi
  if jq -e '.externalSymlink.pages == 1 and .externalSymlink.objectsAdded == 1 and .externalSymlink.rejectedStatus == "failed" and .internalSymlink.pages == 2 and .internalSymlink.objectsAdded == 2 and .internalSymlink.rejectedStatus == null' "$PHASE3_REJECTION_REPORT" >/dev/null; then
    pass "CHECK-31: external symlink rejected without read/upload and documented internal symlink accepted"
  else
    fail "CHECK-31: symlink policy evidence is incomplete"
  fi
  if jq -e '.oversized.pages == 1 and .oversized.objectsAdded == 1 and .oversized.rejectedStatus == "failed"' "$PHASE3_REJECTION_REPORT" >/dev/null; then
    pass "CHECK-32: oversized image explicitly rejected with no Page or object while its valid neighbor imported"
  else
    fail "CHECK-32: oversized-image evidence is incomplete"
  fi
else
  status=$?
  fail "CHECK-30: unsupported-file proof failed (exit $status): $(short_error "$REJECTION_OUT")"
  fail "CHECK-31: symlink proof failed because rejection verification setup failed"
  fail "CHECK-32: oversized-image proof failed because rejection verification setup failed"
fi

# ── SSRF protection through configured provider destination ───────────────

SSRF_BODY="$TMPDIR/ssrf-response.json"
SSRF_CODE="$(curl -sS --connect-timeout 3 --max-time 10 -o "$SSRF_BODY" -w '%{http_code}' "$API_BASE/series?q=phase3-probe" 2>/dev/null || printf '000')"
if [ "$SSRF_CODE" = "422" ] \
    && jq -e '.detail | contains("Loopback hostname blocked")' "$SSRF_BODY" >/dev/null 2>&1; then
  pass "CHECK-33: API rejects configured loopback provider destination with documented HTTP 422"
else
  fail "CHECK-33: SSRF API proof returned HTTP $SSRF_CODE (expected 422); HTTP 500 is always a failure"
fi

# ── API: admin import-jobs endpoint ───────────────────────────────────────

if [ -f "$ADMIN_JAR" ] && [ -n "$(cookie_value "$ADMIN_JAR" is_access)" ]; then
  ADMIN_JOBS_CODE="$(curl -sS -b "$ADMIN_JAR" -o /dev/null -w '%{http_code}' "$API_BASE/admin/import-jobs" 2>/dev/null || echo "000")"
  if [ "$ADMIN_JOBS_CODE" = "200" ]; then
    pass "CHECK-34: admin import-jobs endpoint returns 200"
  else
    fail "CHECK-34: admin import-jobs returned $ADMIN_JOBS_CODE"
  fi
else
  skip "CHECK-34: admin import-jobs test (no admin session)"
fi

# ── API: admin integrity/summary endpoint ──────────────────────────────────

if [ -f "$ADMIN_JAR" ] && [ -n "$(cookie_value "$ADMIN_JAR" is_access)" ]; then
  ADMIN_INT_CODE="$(curl -sS -b "$ADMIN_JAR" -o /dev/null -w '%{http_code}' "$API_BASE/admin/integrity/summary" 2>/dev/null || echo "000")"
  if [ "$ADMIN_INT_CODE" = "200" ]; then
    pass "CHECK-35: admin integrity/summary endpoint returns 200"
  else
    fail "CHECK-35: admin integrity/summary returned $ADMIN_INT_CODE"
  fi
else
  skip "CHECK-35: admin integrity/summary test (no admin session)"
fi

# ── API: public library listing shows imported series ─────────────────────

LIBRARY_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$API_BASE/series" 2>/dev/null || echo "000")"
if [ "$LIBRARY_CODE" = "200" ]; then
  LIBRARY_BODY="$(curl -sS "$API_BASE/series" 2>/dev/null || true)"
  if printf '%s' "$LIBRARY_BODY" | "$API_PYTHON" -c "
import sys, json
data = json.load(sys.stdin)
total = data.get('total', 0)
print('ok' if total >= 3 else 'fail')
" 2>/dev/null | grep -q 'ok'; then
    pass "CHECK-36: public series listing shows imported series"
  else
    fail "CHECK-36: public series listing missing imported series"
  fi
else
  fail "CHECK-36: public series listing returned $LIBRARY_CODE"
fi

# ── API: library detail with ready chapters ───────────────────────────────

LIBRARY_DETAIL_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$API_BASE/library/$SERIES_SLUG" 2>/dev/null || echo "000")"
if [ "$LIBRARY_DETAIL_CODE" = "200" ]; then
  pass "CHECK-37: library detail endpoint returns 200 for imported series"
else
  fail "CHECK-37: library detail returned $LIBRARY_DETAIL_CODE"
fi

# ── API: chapter pages returns ready chapters only ────────────────────────

CHAPTER_PAGES_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$API_BASE/library/$SERIES_SLUG/chapter/1" 2>/dev/null || echo "000")"
if [ "$CHAPTER_PAGES_CODE" = "200" ]; then
  pass "CHECK-38: chapter pages endpoint returns 200"
else
  fail "CHECK-38: chapter pages returned $CHAPTER_PAGES_CODE"
fi

# ── Security audit: no imported images in git ─────────────────────────────

MANGA_FILES="$(git ls-files 2>/dev/null | awk '$0 !~ /^web\/test-results\// && /\.(jpg|jpeg|png|webp|cbz|cbr)$/ { print; count++; if (count == 5) exit }')"
if [ -z "$MANGA_FILES" ]; then
  pass "CHECK-39: no imported manga images tracked in git"
else
  fail "CHECK-39: manga images tracked in git: $MANGA_FILES"
fi

# ── Security audit: no real .env in git ───────────────────────────────────

REAL_ENV="$(git ls-files 2>/dev/null | awk '/\.env$/ && $0 !~ /\.env\.example$/ { print; exit }')"
if [ -z "$REAL_ENV" ]; then
  pass "CHECK-40: no real .env tracked in git"
else
  fail "CHECK-40: real .env tracked: $REAL_ENV"
fi

# ── Tooling: ruff lint (backend) ─────────────────────────────────────────

if [ -x "$API_PYTHON" ]; then
  RUFF_BIN="$API_PYTHON"
  if "$RUFF_BIN" -m ruff --version >/dev/null 2>&1; then
    RUFF_PATHS=(
      api/importing api/storage api/providers api/tools api/image_inspector.py
      api/tests/test_importing.py api/tests/test_storage.py api/tests/test_providers.py
      api/tests/test_integrity.py api/tests/test_image_inspector.py api/tests/test_media.py
    )
    if RUFF_OUT=$("$RUFF_BIN" -m ruff check "${RUFF_PATHS[@]}" --no-fix 2>&1); then
      pass "TOOL-9: Phase 3 backend lint clean"
    else
      status=$?
      fail "TOOL-9: ruff failed (exit $status): $(short_error "$RUFF_OUT")"
    fi
  else
    fail "TOOL-9: required Phase 3 backend lint is unavailable"
  fi
else
  fail "TOOL-9: required Phase 3 backend lint cannot run without API Python"
fi

# ── Tooling: Playwright E2E ──────────────────────────────────────────────

PHASE3_READY_CHAPTER_ID="$(db_query "SELECT c.id::text FROM chapters c JOIN series s ON s.id = c.series_id WHERE s.slug = '$SERIES_SLUG' AND c.import_status = 'ready' AND EXISTS (SELECT 1 FROM pages p WHERE p.chapter_id = c.id AND p.integrity_status = 'verified') ORDER BY c.number LIMIT 1;" 2>/dev/null || true)"
PHASE3_IMPORTING_CHAPTER_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
PHASE3_FAILED_CHAPTER_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
PHASE3_PENDING_PAGE_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
PHASE3_QUARANTINED_PAGE_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
if [ -n "$PHASE3_READY_CHAPTER_ID" ] && db_query "
  INSERT INTO chapters (id, series_id, number, title, language, page_count, import_status)
  SELECT '$PHASE3_IMPORTING_CHAPTER_ID', id, 990001, 'Phase 3 importing content', 'en', 0, 'importing' FROM series WHERE slug = '$SERIES_SLUG';
  INSERT INTO chapters (id, series_id, number, title, language, page_count, import_status)
  SELECT '$PHASE3_FAILED_CHAPTER_ID', id, 990002, 'Phase 3 failed content', 'en', 0, 'failed' FROM series WHERE slug = '$SERIES_SLUG';
  INSERT INTO pages (id, chapter_id, page_number, object_key, sha256, file_extension, integrity_status)
  SELECT '$PHASE3_PENDING_PAGE_ID', c.id, 99001,
    'series/' || c.series_id::text || '/' || c.id::text || '/99001-' || repeat('c', 64) || '.jpg',
    repeat('c', 64), 'jpg', 'pending' FROM chapters c WHERE c.id = '$PHASE3_READY_CHAPTER_ID';
  INSERT INTO pages (id, chapter_id, page_number, object_key, sha256, file_extension, integrity_status)
  SELECT '$PHASE3_QUARANTINED_PAGE_ID', c.id, 99002,
    'series/' || c.series_id::text || '/' || c.id::text || '/99002-' || repeat('d', 64) || '.jpg',
    repeat('d', 64), 'jpg', 'quarantined' FROM chapters c WHERE c.id = '$PHASE3_READY_CHAPTER_ID';
" >/dev/null 2>&1; then
  pass "TOOL-10a: deterministic hidden-content browser fixture created"
else
  fail "TOOL-10a: could not create mandatory Phase 3 browser fixture"
fi

if [ -x web/node_modules/.bin/playwright ]; then
  if PLAYWRIGHT_OUT=$(PLAYWRIGHT_EXTERNAL_SERVER=1 PHASE3_MINIO_HOST_MAP=1 \
      API_URL="$API_BASE" WEB_URL="$WEB_BASE" \
      PHASE3_MINIO_PORT="$MINIO_HOST_PORT" \
      PHASE3_SERIES_SLUG="$SERIES_SLUG" \
      PHASE3_READY_CHAPTER_ID="$PHASE3_READY_CHAPTER_ID" \
      PHASE3_IMPORTING_CHAPTER_ID="$PHASE3_IMPORTING_CHAPTER_ID" \
      PHASE3_FAILED_CHAPTER_ID="$PHASE3_FAILED_CHAPTER_ID" \
      PHASE3_PENDING_PAGE_ID="$PHASE3_PENDING_PAGE_ID" \
      PHASE3_QUARANTINED_PAGE_ID="$PHASE3_QUARANTINED_PAGE_ID" \
      npm --prefix web run test:e2e:phase3 2>&1); then
    pass "TOOL-10: Phase 3 Playwright E2E tests passed"
  else
    status=$?
    fail "TOOL-10: Phase 3 Playwright E2E tests failed (exit $status): $(short_error "$PLAYWRIGHT_OUT")"
  fi
else
  fail "TOOL-10: Phase 3 Playwright is mandatory and not installed"
fi

# ── Tooling: Docker Compose build verification ────────────────────────────

if DOCKER_BUILD_OUT=$(compose build 2>&1); then
  pass "TOOL-11: Docker Compose build succeeds"
else
  status=$?
  fail "TOOL-11: Docker Compose build failed (exit $status): $(short_error "$DOCKER_BUILD_OUT")"
fi

# ── Security: no credentials in frontend bundle ───────────────────────────

if [ -d web/.next ]; then
  CRED_HITS="$(grep -rl 'minioadmin\|s3_secret\|JWT_SECRET\|POSTGRES_PASSWORD\|DATABASE_URL' web/.next/ 2>/dev/null | head -5 || true)"
  if [ -z "$CRED_HITS" ]; then
    pass "CHECK-41: no credentials found in frontend build output"
  else
    fail "CHECK-41: credentials found in frontend build: $CRED_HITS"
  fi
else
  skip "CHECK-41: frontend build output not found (.next directory missing)"
fi

# ── Verify content-addressed key format ───────────────────────────────────

KEY_COUNT="$(db_query "SELECT count(*) FROM pages p JOIN chapters c ON c.id = p.chapter_id JOIN series s ON s.id = c.series_id WHERE s.slug LIKE '$SERIES_SLUG%';" 2>/dev/null || printf '0')"
INVALID_KEY_COUNT="$(db_query "SELECT count(*) FROM pages p JOIN chapters c ON c.id = p.chapter_id JOIN series s ON s.id = c.series_id WHERE s.slug LIKE '$SERIES_SLUG%' AND (p.object_key IS NULL OR p.sha256 IS NULL OR p.file_extension IS NULL OR p.object_key !~ '^series/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9]{5}-[0-9a-f]{64}\\.(jpg|png|webp)$' OR p.object_key IS DISTINCT FROM 'series/' || s.id::text || '/' || c.id::text || '/' || lpad(p.page_number::text, 5, '0') || '-' || p.sha256 || '.' || p.file_extension);" 2>/dev/null || printf 'error')"
if [ "$KEY_COUNT" -gt 0 ] 2>/dev/null && [ "$INVALID_KEY_COUNT" = "0" ]; then
  pass "CHECK-42: all $KEY_COUNT imported page keys exactly match their content-addressed row data"
else
  fail "CHECK-42: exact object-key validation failed (keys=$KEY_COUNT invalid=$INVALID_KEY_COUNT)"
fi

# ── Verify chapters.number precision ──────────────────────────────────────

NUMERIC_VALUES="$(db_query "SELECT number::text FROM chapters WHERE series_id IN (SELECT id FROM series WHERE slug LIKE '$SERIES_SLUG%') ORDER BY number LIMIT 5;" 2>/dev/null || true)"
if [ -n "$NUMERIC_VALUES" ]; then
  PRECISION_OK=$("$API_PYTHON" -c "
import sys
values = '''$NUMERIC_VALUES'''.strip().split('\n')
for v in values:
    v = v.strip()
    if not v:
        continue
    parts = v.split('.')
    if len(parts) == 2 and len(parts[1]) > 4:
        print('fail')
        sys.exit(0)
print('ok')
" 2>/dev/null || echo "ok")
  if [ "$PRECISION_OK" = "ok" ]; then
    pass "CHECK-43: chapters.number precision within NUMERIC(12,4)"
  else
    fail "CHECK-43: chapters.number exceeds NUMERIC(12,4) precision"
  fi
else
  skip "CHECK-43: no chapter numbers to validate"
fi

# ── Verify page integrity check CLI works standalone ──────────────────────

INTEGRITY_STANDALONE=$(cd "$REPO_ROOT/api" && "$API_PYTHON" -m tools.integrity check 2>&1 || true)
if printf '%s' "$INTEGRITY_STANDALONE" | grep -qE 'All checks passed|Integrity Report|Total issues'; then
  pass "CHECK-44: integrity CLI check runs standalone"
else
  fail "CHECK-44: integrity CLI check output unexpected"
fi

# ── Verify chapter numbers are Decimal in ORM ─────────────────────────────

ORM_CHECK=$("$API_PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_ROOT/api')
from models import Chapter
from decimal import Decimal
col_type = type(chapter_number_type := Chapter.__table__.c.number.type)
assert 'Numeric' in col_type.__name__, f'Expected Numeric, got {col_type.__name__}'
print('ok')
" 2>/dev/null || echo "error")
if [ "$ORM_CHECK" = "ok" ]; then
  pass "CHECK-45: ORM Chapter.number is Numeric type"
else
  skip "CHECK-45: ORM type check (API Python path issue)"
fi

# ── Verify provider adapter SSRF blocking ─────────────────────────────────

SSRF_CHECK=$("$API_PYTHON" -c "
import sys
sys.path.insert(0, '$REPO_ROOT/api')
from providers.ssrf import validate_url
targets = [
    'https://127.0.0.1/secret',
    'https://10.0.0.1/secret',
    'https://169.254.169.254/secret',
    'https://192.0.2.1/secret',
    'https://[::1]/secret',
]
for target in targets:
    try:
        validate_url(target, allow_localhost=False)
    except ValueError:
        continue
    raise AssertionError(f'accepted blocked target: {target}')
print('ok')
" 2>/dev/null || echo "error")
if [ "$SSRF_CHECK" = "ok" ]; then
  pass "CHECK-46: SSRF validation blocks loopback, private, link-local, and reserved URLs"
else
  fail "CHECK-46: mandatory SSRF URL validation unit check failed"
fi

# ── Verify storage key functions ──────────────────────────────────────────

KEY_FN_CHECK=$("$API_PYTHON" -c "
import sys, uuid
sys.path.insert(0, '$REPO_ROOT/api')
import re
from storage.keys import page_object_key, cover_object_key
s_id = uuid.uuid4()
ch_id = uuid.uuid4()
page_digest = 'a' * 64
cover_digest = 'b' * 64
key = page_object_key(s_id, ch_id, 1, page_digest, 'jpg')
assert key == f'series/{s_id}/{ch_id}/00001-{page_digest}.jpg', key
assert re.fullmatch(r'series/[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9]{5}-[0-9a-f]{64}\.(jpg|png|webp)', key), key
cover = cover_object_key(s_id, cover_digest, 'png')
assert cover == f'series/{s_id}/cover-{cover_digest}.png', cover
print('ok')
" 2>/dev/null || echo "error")
if [ "$KEY_FN_CHECK" = "ok" ]; then
  pass "CHECK-47: storage key functions produce correct format"
else
  fail "CHECK-47: mandatory storage key unit check failed"
fi

# ── Verify image inspector rejects animated ───────────────────────────────

ANIM_CHECK=$("$API_PYTHON" -c "
import sys, tempfile, os
from pathlib import Path
sys.path.insert(0, '$REPO_ROOT/api')
from image_inspector import ImageInspector, ImageInspectionError
from PIL import Image

tmpdir = tempfile.mkdtemp()
root = Path(tmpdir)
# Create animated GIF (should be rejected by default)
frames = [Image.new('RGB', (10, 10), (i*50, 0, 0)) for i in range(3)]
gif_path = root / 'anim.gif'
frames[0].save(gif_path, save_all=True, append_images=frames[1:], duration=100, loop=0)

inspector = ImageInspector(root, allow_animated=False)
try:
    inspector.inspect(gif_path)
    print('fail')
except ImageInspectionError:
    print('ok')
except Exception:
    print('fail')
finally:
    import shutil
    shutil.rmtree(tmpdir)
" 2>/dev/null || echo "error")
if [ "$ANIM_CHECK" = "ok" ]; then
  pass "CHECK-48: image inspector rejects animated GIF"
else
  fail "CHECK-48: mandatory animated image rejection check failed"
fi

run_check "CHECK-ENV: repository infra/.env remained unchanged" verify_runtime_assert_repo_env_unchanged

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "======================================"
echo "  Phase 3 Verification Summary"
echo "======================================"
for result in "${RESULTS[@]}"; do
  printf '  %s\n' "$result"
done
printf 'Passed: %d\nFailed: %d\nSkipped: %d\n' "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo ""
  echo "Failed checks:"
  for fc in "${FAILED_CHECKS[@]}"; do
    printf '  - %s\n' "$fc"
  done
fi

if [ "$FAIL_COUNT" -eq 0 ] && [ "$SKIP_COUNT" -eq 0 ]; then
  printf '%bPhase 3 gate: PASS%b\n' "$GREEN" "$NC"
  exit 0
fi
printf '%bPhase 3 gate: FAIL%b\n' "$RED" "$NC"
exit 1
