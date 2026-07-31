#!/usr/bin/env bash
# InfinityScan Phase 5 release gate.
#
# Every check is mandatory. Missing tooling or unavailable disposable
# infrastructure is a failure, never a skip. This script never accepts a
# production environment and all Docker/database operations use synthetic data.
# shellcheck disable=SC2016
# shellcheck disable=SC2329 # Commands and cleanup handlers are invoked indirectly.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

if [ "${APP_ENV:-development}" = "production" ]; then
  printf '%s\n' 'Phase 5 refuses APP_ENV=production.' >&2
  exit 2
fi
. "$REPO_ROOT/scripts/lib/verify-runtime.sh"
verify_runtime_init phase5
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
TMPDIR="$VERIFY_TMPDIR"
if [ -n "${PYTHON:-}" ]; then
  API_PYTHON="$PYTHON"
elif [ -x "$REPO_ROOT/api/.venv/bin/python" ]; then
  API_PYTHON="$REPO_ROOT/api/.venv/bin/python"
elif [ -x /tmp/infinityscan-audit-venv/bin/python ]; then
  API_PYTHON=/tmp/infinityscan-audit-venv/bin/python
else
  API_PYTHON="$(command -v python3 || true)"
fi
STACK_STARTED=false
STACK_READY=false
MIGRATION_STARTED=false
MIGRATION_READY=false
MIGRATION_COMPOSE=()

# shellcheck disable=SC2329
cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [ "$MIGRATION_STARTED" = true ] && [ "${#MIGRATION_COMPOSE[@]}" -gt 0 ]; then
    "${MIGRATION_COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
  fi
  verify_runtime_cleanup "$status"
  exit $?
}
trap cleanup EXIT

pass() { PASS_COUNT=$((PASS_COUNT + 1)); printf '%bPASS%b  %s\n' "$GREEN" "$NC" "$1"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf '%bFAIL%b  %s\n' "$RED" "$NC" "$1"; }

run_check() {
  local label="$1"
  shift
  local output status
  if output=$("$@" 2>&1); then
    pass "$label"
  else
    status=$?
    printf '%s\n' "$output" | tail -n 8 >&2
    fail "$label (exit $status)"
    return "$status"
  fi
}

run_shell_check() {
  local label="$1" command="$2"
  run_check "$label" bash -c "$command"
}

wait_http() {
  local url="$1"
  for _ in $(seq 1 30); do
    if curl -fsS "$url" >/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

no_backup_artifacts() {
  [ ! -d backups ] && [ -z "$(git ls-files | awk 'tolower($0) ~ /(^|\/)(backups?\/|.*\.dump(\.age)?$|.*\.dump\.manifest\.json$)/ {print}')" ]
}

run_test_no_skips() {
  local label="$1"
  shift
  local output status
  output=$("$@" 2>&1) || status=$?
  status="${status:-0}"
  if [ "$status" -eq 0 ] && ! printf '%s\n' "$output" | grep -Eq '(^|[[:space:]])[1-9][0-9]* skipped([,[:space:]]|$)'; then
    pass "$label"
  else
    printf '%s\n' "$output" | tail -n 8 >&2
    fail "$label"
    return 1
  fi
}

finish() {
  printf '\nPassed: %d\nFailed: %d\nSkipped: %d\n' "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
  if [ "$FAIL_COUNT" -eq 0 ] && [ "$SKIP_COUNT" -eq 0 ]; then
    printf 'Phase 5 gate: PASS\n'
    exit 0
  fi
  printf 'Phase 5 gate: FAIL\n'
  exit 1
}

run_gate() {
  local phase="$1" script="$2" log status
  shift 2
  status=0
  log="$TMPDIR/phase-${phase}.log"
  bash "$script" "$@" >"$log" 2>&1 || status=$?
  if [ "$status" -eq 0 ] \
    && grep -q '^Failed: 0$' "$log" \
    && grep -q '^Skipped: 0$' "$log" \
    && grep -q "Phase ${phase} gate: PASS" "$log"; then
    pass "PREREQUISITE: Phase ${phase} gate"
    return 0
  fi
  printf '%s\n' "Phase ${phase} output:" >&2
  tail -n 20 "$log" >&2
  fail "PREREQUISITE: Phase ${phase} gate"
  if [ "$status" -ne 0 ]; then
    return "$status"
  fi
  return 1
}

printf '%s\n' '======================================'
printf '%s\n' '  InfinityScan Phase 5 Verification'
printf '%s\n' '======================================'

PHASE_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --phase-only) PHASE_ONLY=true ;;
    *) fail "ARGUMENTS: unknown argument: $arg" ;;
  esac
done

# Prerequisites are intentionally sequential and fail immediately.
if [ "$PHASE_ONLY" = false ]; then
  run_gate 1 scripts/verify-phase1.sh || finish
  run_gate 2 scripts/verify-phase2.sh || finish
  run_gate 3 scripts/verify-phase3.sh --phase-only || finish
  run_gate 4 scripts/verify-phase4.sh --phase-only || finish
fi

# 1. Required tools.
# shellcheck disable=SC2016
run_shell_check 'CHECK-01: required tools available' 'for tool in python3 node npm git jq docker curl shellcheck actionlint pg_dump pg_restore age age-keygen; do command -v "$tool" >/dev/null || exit 1; done'
run_shell_check 'CHECK-01a: shell syntax' 'bash -n scripts/*.sh scripts/lib/*.sh'
run_check 'CHECK-01b: shellcheck' shellcheck scripts/lib/verify-runtime.sh scripts/test-disaster-recovery.sh scripts/verify-phase5.sh scripts/backup-db.sh scripts/restore-db.sh scripts/lib/npm-audit.sh
run_check 'CHECK-01c: verifier disk and inode preflight' verify_runtime_preflight || finish

# 2-5. Repository safety and generated-artifact policy.
run_check 'CHECK-02a: repository infra/.env remained unchanged' verify_runtime_assert_repo_env_unchanged
run_shell_check 'CHECK-02: no real .env tracked' '[ -z "$(git ls-files | awk '\''/\.env$/ && $0 !~ /\.env\.example$/ {print}'\'')" ]'
run_check 'CHECK-03: no backup artifacts tracked or present' no_backup_artifacts
run_shell_check 'CHECK-04: no private keys tracked' '[ -z "$(git ls-files | awk '\''tolower($0) ~ /(^|\/)(id_rsa|id_ed25519)$|\.(pem|key|p12|pfx)$/ {print}'\'')" ]'
run_shell_check 'CHECK-05: no generated manga content tracked' '[ -z "$(git ls-files | awk '\''tolower($0) ~ /\.(jpg|jpeg|png|webp|avif|gif|cbz|cbr|pdf)$/ && $0 !~ /^web\/public\/ {print}'\'')" ]'

# 6-14. Backend/frontend/security fast checks.
run_test_no_skips 'CHECK-06: backend pytest' "$API_PYTHON" -m pytest api/tests --ignore=api/tests/integration/test_storage_minio.py -q
run_check 'CHECK-07: frontend tests' npm --prefix web test
run_check 'CHECK-08: ruff' "$API_PYTHON" -m ruff check api/importing api/storage api/providers api/tools api/audit_events.py api/middleware.py scripts/backup_db.py scripts/dr_fixture.py
run_check 'CHECK-09: ESLint' npm --prefix web run lint
run_check 'CHECK-10: TypeScript' web/node_modules/.bin/tsc --noEmit -p web/tsconfig.json
run_check 'CHECK-11: production frontend build' npm --prefix web run build
run_check 'CHECK-12: pip-audit runtime' "$API_PYTHON" -m pip_audit -r api/requirements.txt
run_check 'CHECK-13: pip-audit development' "$API_PYTHON" -m pip_audit -r api/requirements-dev.txt
run_check 'CHECK-14: shared npm audit policy' scripts/lib/npm-audit.sh web scripts/lib/npm-audit-exceptions.json "$TMPDIR/npm-audit.json"

# 15-21. Migration graph, drift, and database constraints.
run_shell_check 'CHECK-15: single Alembic head' "cd api && \"$API_PYTHON\" -c 'from pathlib import Path; from tools.migration_graph import inspect_graph; assert inspect_graph(Path(\"alembic/versions\")).ok'"
run_shell_check 'CHECK-16: migration manifest valid' "cd api && \"$API_PYTHON\" -m tools.migration_verifier check --json | jq -e '.ok == true' >/dev/null"
run_shell_check 'CHECK-17: historical migration hashes valid' "cd api && \"$API_PYTHON\" -m tools.migration_verifier check --json | jq -e '.findings == []' >/dev/null"
MIGRATION_DB_USER="phase5_user"
MIGRATION_DB_PASSWORD="phase5_password_${BASHPID}"
MIGRATION_DB_NAME="phase5_migrations_${BASHPID}"
MIGRATION_SCRATCH_NAME="phase5_scratch_${BASHPID}"
MIGRATION_DB_PORT=""
MIGRATION_ENV="$TMPDIR/migration.env"
MIGRATION_OVERRIDE="$TMPDIR/migration.override.yml"
MIGRATION_DATABASE_URL=""
MIGRATION_SCRATCH_URL=""
MIGRATION_PROJECT="${VERIFY_PROJECT}-migrations"

prepare_migration_database() {
  cp infra/.env.example "$MIGRATION_ENV"
  sed -i "s/^POSTGRES_USER=.*/POSTGRES_USER=$MIGRATION_DB_USER/; s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$MIGRATION_DB_PASSWORD/; s/^POSTGRES_DB=.*/POSTGRES_DB=$MIGRATION_DB_NAME/" "$MIGRATION_ENV"
  printf 'MINIO_ROOT_PASSWORD=unused_%s\n' "$VERIFY_RUN_ID" >>"$MIGRATION_ENV"
  cat >"$MIGRATION_OVERRIDE" <<EOF
services:
  db:
    ports:
      - "127.0.0.1::5432"
EOF
  MIGRATION_COMPOSE=(
    env
    "POSTGRES_USER=$MIGRATION_DB_USER"
    "POSTGRES_PASSWORD=$MIGRATION_DB_PASSWORD"
    "POSTGRES_DB=$MIGRATION_DB_NAME"
    "DATABASE_URL=postgresql+psycopg://${MIGRATION_DB_USER}:${MIGRATION_DB_PASSWORD}@db:5432/${MIGRATION_DB_NAME}"
    "JWT_SECRET_KEY=$(verify_runtime_env_value JWT_SECRET_KEY)"
    "CSRF_SECRET_KEY=$(verify_runtime_env_value CSRF_SECRET_KEY)"
    "MINIO_ROOT_PASSWORD=unused_${VERIFY_RUN_ID}"
    "INFINITYSCAN_ENV_FILE=$MIGRATION_ENV"
    docker compose --project-name "$MIGRATION_PROJECT" --env-file "$MIGRATION_ENV"
    -f "$REPO_ROOT/infra/docker-compose.yml" -f "$MIGRATION_OVERRIDE"
  )
  "${MIGRATION_COMPOSE[@]}" up -d db >/dev/null || return 1
  MIGRATION_STARTED=true
  MIGRATION_DB_PORT="$("${MIGRATION_COMPOSE[@]}" port db 5432 | awk -F: 'NR == 1 { print $NF }')"
  [ -n "$MIGRATION_DB_PORT" ] || return 1
  MIGRATION_DATABASE_URL="postgresql+psycopg://${MIGRATION_DB_USER}:${MIGRATION_DB_PASSWORD}@127.0.0.1:${MIGRATION_DB_PORT}/${MIGRATION_DB_NAME}"
  MIGRATION_SCRATCH_URL="postgresql+psycopg://${MIGRATION_DB_USER}:${MIGRATION_DB_PASSWORD}@127.0.0.1:${MIGRATION_DB_PORT}/${MIGRATION_SCRATCH_NAME}"
  local container=""
  for _ in $(seq 1 60); do
    container="$("${MIGRATION_COMPOSE[@]}" ps -q db | tr -d '[:space:]')"
    if [ -n "$container" ] && docker exec "$container" pg_isready -U "$MIGRATION_DB_USER" -d "$MIGRATION_DB_NAME" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  [ -n "$container" ] && docker exec "$container" pg_isready -U "$MIGRATION_DB_USER" -d "$MIGRATION_DB_NAME" >/dev/null || return 1
  docker exec -e "PGPASSWORD=$MIGRATION_DB_PASSWORD" "$container" psql -h localhost -U "$MIGRATION_DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE \"$MIGRATION_SCRATCH_NAME\"" >/dev/null || return 1
  MIGRATION_READY=true
}

check_orm_drift() {
  [ "$MIGRATION_READY" = true ] || return 1
  (cd api && DATABASE_URL="$MIGRATION_DATABASE_URL" "$API_PYTHON" -m alembic upgrade head && DATABASE_URL="$MIGRATION_DATABASE_URL" "$API_PYTHON" -m alembic check)
}

check_clean_migration() {
  local status=0
  [ "$MIGRATION_READY" = true ] || return 1
  (cd api && DATABASE_URL="$MIGRATION_DATABASE_URL" "$API_PYTHON" -m tools.migration_verifier check \
    --database-url "$MIGRATION_DATABASE_URL" --scratch-url "$MIGRATION_SCRATCH_URL" --require-db-head --require-schema --json | jq -e '.ok == true' >/dev/null) || return 1
  INFINITYSCAN_DISPOSABLE_MIGRATION_TEST=1 \
    DATABASE_URL="$MIGRATION_DATABASE_URL" \
    TASK6_SECOND_DATABASE_URL="$MIGRATION_SCRATCH_URL" \
    "$API_PYTHON" -m pytest api/tests_postgresql/test_task6_migrations.py -q || status=$?
  (cd api && DATABASE_URL="$MIGRATION_DATABASE_URL" "$API_PYTHON" -m alembic upgrade head) || return 1
  return "$status"
}

check_downgrade_upgrade() {
  [ "$MIGRATION_READY" = true ] || return 1
  (cd api && DATABASE_URL="$MIGRATION_DATABASE_URL" "$API_PYTHON" -m alembic downgrade 0007 && DATABASE_URL="$MIGRATION_DATABASE_URL" "$API_PYTHON" -m alembic upgrade head)
}

check_postgresql_constraints() {
  [ "$MIGRATION_READY" = true ] || return 1
  INFINITYSCAN_DISPOSABLE_TEST=1 DATABASE_URL="$MIGRATION_DATABASE_URL" \
    "$API_PYTHON" -m pytest api/tests_postgresql/test_phase5_schema.py -q
}

prepare_migration_database || true
run_check 'CHECK-18: alembic check has no ORM drift' check_orm_drift
run_check 'CHECK-19: clean base-to-head migration' check_clean_migration
run_check 'CHECK-20: newest downgrade/upgrade' check_downgrade_upgrade
run_check 'CHECK-21: database constraints' check_postgresql_constraints

# 22-40. Integrity, recovery, sessions, auth, and audit regression ownership.
run_test_no_skips 'CHECK-22: quick integrity clean fixture' "$API_PYTHON" -m pytest api/tests/test_integrity.py -q
run_test_no_skips 'CHECK-23: full integrity clean fixture' "$API_PYTHON" -m pytest api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-24: missing object detection' "$API_PYTHON" -m pytest api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-25: object metadata mismatch detection' "$API_PYTHON" -m pytest api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-26: orphan object detection' "$API_PYTHON" -m pytest api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-27: repair-safe scope' "$API_PYTHON" -m pytest api/tests/test_integrity.py api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-28: stale import detection' env PYTHONPATH="$REPO_ROOT/api" "$API_PYTHON" -m pytest api/tests_phase5/test_import_recovery.py -q
run_test_no_skips 'CHECK-29: safe import recovery' env PYTHONPATH="$REPO_ROOT/api" "$API_PYTHON" -m pytest api/tests_phase5/test_import_recovery.py -q
run_test_no_skips 'CHECK-30: active import fencing' env PYTHONPATH="$REPO_ROOT/api" "$API_PYTHON" -m pytest api/tests_phase5/test_import_recovery.py -q
run_test_no_skips 'CHECK-31: session cleanup dry-run' "$API_PYTHON" -m pytest api/tests/test_sessions_tool.py -q
run_test_no_skips 'CHECK-32: active sessions preserved' "$API_PYTHON" -m pytest api/tests/test_sessions_tool.py -q
run_test_no_skips 'CHECK-33: expired/revoked cleanup' "$API_PYTHON" -m pytest api/tests/test_sessions_tool.py -q
run_test_no_skips 'CHECK-34: authentication regression suite' "$API_PYTHON" -m pytest api/tests/test_auth.py api/tests/test_session.py -q
run_test_no_skips 'CHECK-35: security boundary regression suite' "$API_PYTHON" -m pytest api/tests/test_task5_security.py api/tests/test_deps.py -q
run_test_no_skips 'CHECK-36: audit insertion' "$API_PYTHON" -m pytest api/tests/test_task7_audit.py -q
run_test_no_skips 'CHECK-37: forbidden audit metadata rejected' "$API_PYTHON" -m pytest api/tests/test_task7_audit.py -q
run_test_no_skips 'CHECK-38: audit read requires admin' "$API_PYTHON" -m pytest api/tests/test_task7_audit.py -q
run_test_no_skips 'CHECK-39: normal user cannot read audit events' "$API_PYTHON" -m pytest api/tests/test_task7_audit.py -q
run_test_no_skips 'CHECK-40: audit pruning dry-run' "$API_PYTHON" -m pytest api/tests/test_task7_audit.py -q

# 41-49. One complete disposable DR drill proves all backup and restore checks.
DR_LOG="$TMPDIR/disaster-recovery.log"
DR_STATUS=0
scripts/test-disaster-recovery.sh >"$DR_LOG" 2>&1 || DR_STATUS=$?
if [ "$DR_STATUS" -eq 0 ]; then
  for check in \
    'CHECK-41: encrypted database backup succeeds' \
    'CHECK-42: backup checksum verifies' \
    'CHECK-43: backup metadata contains no secrets' \
    'CHECK-44: disaster-recovery restore drill passes' \
    'CHECK-45: restored Alembic revision equals head' \
    'CHECK-46: restored row counts match baseline' \
    'CHECK-47: restored object storage integrity passes' \
    'CHECK-48: sample login after restore works' \
    'CHECK-49: sample bookmark/progress survives restore'; do pass "$check"; done
else
  tail -n 20 "$DR_LOG" >&2
  for check in \
    'CHECK-41: encrypted database backup succeeds' \
    'CHECK-42: backup checksum verifies' \
    'CHECK-43: backup metadata contains no secrets' \
    'CHECK-44: disaster-recovery restore drill passes' \
    'CHECK-45: restored Alembic revision equals head' \
    'CHECK-46: restored row counts match baseline' \
    'CHECK-47: restored object storage integrity passes' \
    'CHECK-48: sample login after restore works' \
    'CHECK-49: sample bookmark/progress survives restore'; do fail "$check"; done
fi

# 50-55. Workflow and Dependabot policy.
run_check 'CHECK-50: GitHub Actions YAML parses' "$API_PYTHON" scripts/check-workflows.py
run_check 'CHECK-51: actionlint passes' actionlint .github/workflows/*.yml
run_check 'CHECK-52: workflow permissions are least privilege' "$API_PYTHON" scripts/check-workflows.py
run_check 'CHECK-53: all actions use immutable pins' "$API_PYTHON" scripts/check-workflows.py
run_shell_check 'CHECK-54: no privileged pull_request_target execution' '! git grep -n pull_request_target -- .github/workflows'
run_check 'CHECK-55: Dependabot config validates' "$API_PYTHON" scripts/check-workflows.py

# 56-68. Disposable production image/stack checks.
GATE_COMPOSE=("${VERIFY_COMPOSE[@]}")
GATE_API_URL="http://127.0.0.1:0"
GATE_WEB_URL="http://127.0.0.1:0"
GATE_API_IMAGE="${VERIFY_PROJECT}-api:gate"
GATE_WEB_IMAGE="${VERIFY_PROJECT}-web:gate"
run_check 'CHECK-56: production Compose config' "${GATE_COMPOSE[@]}" config
if run_check 'CHECK-57: API production image builds' docker build -t "$GATE_API_IMAGE" api; then
  verify_runtime_register_image "$GATE_API_IMAGE"
fi
if run_check 'CHECK-58: web production image builds' docker build --build-arg API_INTERNAL_URL=http://api:8000 -t "$GATE_WEB_IMAGE" web; then
  verify_runtime_register_image "$GATE_WEB_IMAGE"
fi
run_shell_check 'CHECK-59: production images contain no tracked secrets' '! git grep -n -E "-----BEGIN .*PRIVATE KEY|postgresql[^ ]*://[^ ]+:[^ ]+@" -- api web'
run_check 'CHECK-60: API image has no real .env' docker run --rm "$GATE_API_IMAGE" sh -c 'test ! -e /app/.env'
run_check 'CHECK-61: production images use non-root users' bash -c 'test "$(docker image inspect "$1" --format "{{.Config.User}}")" = appuser && test "$(docker image inspect "$2" --format "{{.Config.User}}")" = nextjs' bash "$GATE_API_IMAGE" "$GATE_WEB_IMAGE"
if docker info >/dev/null 2>&1; then
  STACK_STARTED=true
  STACK_CHECKS_OK=true
  STACK_START_OK=false
  if run_check 'CHECK-62: Docker stack starts' "${GATE_COMPOSE[@]}" up -d db migrate api web; then
    STACK_START_OK=true
    GATE_API_PORT="$(verify_runtime_port api 8000 || true)"
    GATE_WEB_PORT="$(verify_runtime_port web 3000 || true)"
    GATE_API_URL="http://127.0.0.1:${GATE_API_PORT}"
    GATE_WEB_URL="http://127.0.0.1:${GATE_WEB_PORT}"
  else
    STACK_CHECKS_OK=false
  fi
  database_ready() {
    for _ in $(seq 1 60); do
      if "${GATE_COMPOSE[@]}" exec -T db pg_isready -U "$(verify_runtime_env_value POSTGRES_USER)" -d "$(verify_runtime_env_value POSTGRES_DB)" >/dev/null 2>&1; then
        return 0
      fi
      sleep 1
    done
    return 1
  }
  migration_finished() {
    local migration_container state
    for _ in $(seq 1 120); do
      migration_container="$("${GATE_COMPOSE[@]}" ps -aq migrate | tr -d '[:space:]')"
      if [ -n "$migration_container" ]; then
        state="$(docker inspect "$migration_container" --format '{{.State.Status}}:{{.State.ExitCode}}' 2>/dev/null || true)"
        case "$state" in
          exited:0) return 0 ;;
          exited:*) return 1 ;;
        esac
      fi
      sleep 1
    done
    return 1
  }
  if [ "$STACK_START_OK" = true ]; then
    run_check 'CHECK-63: database becomes healthy' database_ready || STACK_CHECKS_OK=false
    run_check 'CHECK-64: migrations finish' migration_finished || STACK_CHECKS_OK=false
    run_check 'CHECK-65: API health returns 200' wait_http "$GATE_API_URL/health" || STACK_CHECKS_OK=false
    run_check 'CHECK-66: frontend returns 200' wait_http "$GATE_WEB_URL" || STACK_CHECKS_OK=false
  else
    for check in 'CHECK-63: database becomes healthy' 'CHECK-64: migrations finish' 'CHECK-65: API health returns 200' 'CHECK-66: frontend returns 200'; do fail "$check"; done
  fi
  if [ "$STACK_CHECKS_OK" = true ]; then
    STACK_READY=true
  fi
else
  for check in 'CHECK-62: Docker stack starts' 'CHECK-63: database becomes healthy' 'CHECK-64: migrations finish' 'CHECK-65: API health returns 200' 'CHECK-66: frontend returns 200'; do fail "$check"; done
fi
run_shell_check 'CHECK-67: frontend bundle contains no credentials' 'test -d web/.next && ! grep -R -E "DATABASE_URL|POSTGRES_PASSWORD|S3_SECRET_ACCESS_KEY|JWT_SECRET_KEY" web/.next'
gate_logs_safe() {
  local logs
  logs="$("${GATE_COMPOSE[@]}" logs --no-color 2>/dev/null)" || return 1
  ! printf '%s\n' "$logs" | grep -Eiq 'password=|secret_key=|BEGIN .*PRIVATE KEY'
}
if [ "$STACK_READY" = true ]; then
  run_check 'CHECK-68: generated logs contain no obvious secrets' gate_logs_safe
else
  fail 'CHECK-68: generated logs contain no obvious secrets'
fi

# 69-70. Final hygiene and cleanup.
run_check 'CHECK-69: git diff --check' git diff --check
if [ "$FAIL_COUNT" -ne 0 ]; then
  verify_runtime_capture_diagnostics
fi
CLEANUP_OK=true
if [ "$STACK_STARTED" = true ]; then
  "${GATE_COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || CLEANUP_OK=false
  STACK_STARTED=false
fi
if [ "$MIGRATION_STARTED" = true ]; then
  if "${MIGRATION_COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1; then
    MIGRATION_STARTED=false
  else
    CLEANUP_OK=false
  fi
fi
cleanup_complete() {
  [ "$CLEANUP_OK" = true ] \
    && verify_runtime_project_empty "$VERIFY_PROJECT" \
    && verify_runtime_project_empty "$MIGRATION_PROJECT" \
    && verify_runtime_assert_repo_env_unchanged
}
run_check 'CHECK-70: disposable cleanup complete' cleanup_complete

finish
