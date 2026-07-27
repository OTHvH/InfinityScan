#!/usr/bin/env bash
# InfinityScan Phase 5 release gate.
#
# Every check is mandatory. Missing tooling or unavailable disposable
# infrastructure is a failure, never a skip. This script never accepts a
# production environment and all Docker/database operations use synthetic data.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ "${APP_ENV:-development}" = "production" ]; then
  printf '%s\n' 'Phase 5 refuses APP_ENV=production.' >&2
  exit 2
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
TMPDIR="$(mktemp -d /tmp/infinityscan-phase5-XXXXXX)"
if [ -n "${PYTHON:-}" ]; then
  API_PYTHON="$PYTHON"
elif [ -x "$REPO_ROOT/api/.venv/bin/python" ]; then
  API_PYTHON="$REPO_ROOT/api/.venv/bin/python"
else
  API_PYTHON="$REPO_ROOT/.venv/bin/python"
fi
STACK_STARTED=false

cleanup() {
  local status=$?
  set +e
  if [ "$STACK_STARTED" = true ] && [ -n "${GATE_COMPOSE+x}" ]; then
    "${GATE_COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
  fi
  rm -rf "$TMPDIR"
  exit "$status"
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
  fi
}

run_shell_check() {
  local label="$1" command="$2"
  run_check "$label" bash -c "$command"
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
  local phase="$1" script="$2" log="$TMPDIR/phase-${phase}.log" status=0
  bash "$script" >"$log" 2>&1 || status=$?
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
  finish
}

printf '%s\n' '======================================'
printf '%s\n' '  InfinityScan Phase 5 Verification'
printf '%s\n' '======================================'

# Prerequisites are intentionally sequential and fail immediately.
run_gate 1 scripts/verify-phase1.sh
run_gate 2 scripts/verify-phase2.sh
run_gate 3 scripts/verify-phase3.sh
run_gate 4 scripts/verify-phase4.sh

# 1. Required tools.
run_shell_check 'CHECK-01: required tools available' 'for tool in python3 node npm git jq docker curl shellcheck actionlint pg_dump pg_restore age age-keygen; do command -v "$tool" >/dev/null || exit 1; done'

# 2-5. Repository safety and generated-artifact policy.
run_shell_check 'CHECK-02: no real .env tracked' '[ -z "$(git ls-files | awk '\''/\.env$/ && $0 !~ /\.env\.example$/ {print}'\'')" ]'
run_shell_check 'CHECK-03: no backup artifacts tracked or present' '[ -z "$(git ls-files | awk '\''tolower($0) ~ /(^|\/)(backups?\/|.*\.dump(\.age)?$|.*\.dump\.manifest\.json$)/ {print}'\'') ] && [ ! -d backups ]'
run_shell_check 'CHECK-04: no private keys tracked' '[ -z "$(git ls-files | awk '\''tolower($0) ~ /(^|\/)(id_rsa|id_ed25519)$|\.(pem|key|p12|pfx)$/ {print}'\'')" ]'
run_shell_check 'CHECK-05: no generated manga content tracked' '[ -z "$(git ls-files | awk '\''tolower($0) ~ /\.(jpg|jpeg|png|webp|avif|gif|cbz|cbr|pdf)$/ && $0 !~ /^web\/public\/ {print}'\'')" ]'

# 6-14. Backend/frontend/security fast checks.
run_test_no_skips 'CHECK-06: backend pytest' "$API_PYTHON" -m pytest api/tests -q
run_check 'CHECK-07: frontend tests' npm --prefix web test
run_check 'CHECK-08: ruff' "$API_PYTHON" -m ruff check api/importing api/storage api/providers api/tools api/audit_events.py api/middleware.py scripts/backup_db.py scripts/dr_fixture.py
run_check 'CHECK-09: ESLint' npm --prefix web run lint
run_check 'CHECK-10: TypeScript' npm --prefix web exec -- tsc --noEmit
run_check 'CHECK-11: production frontend build' npm --prefix web run build
run_check 'CHECK-12: pip-audit runtime' "$API_PYTHON" -m pip_audit -r api/requirements.txt
run_check 'CHECK-13: pip-audit development' "$API_PYTHON" -m pip_audit -r api/requirements-dev.txt
run_check 'CHECK-14: shared npm audit policy' scripts/lib/npm-audit.sh web scripts/lib/npm-audit-exceptions.json "$TMPDIR/npm-audit.json"

# 15-21. Migration graph, drift, and database constraints.
run_shell_check 'CHECK-15: single Alembic head' "cd api && \"$API_PYTHON\" -c 'from pathlib import Path; from tools.migration_graph import inspect_graph; assert inspect_graph(Path(\"alembic/versions\")).ok'"
run_shell_check 'CHECK-16: migration manifest valid' "cd api && \"$API_PYTHON\" -m tools.migration_verifier check --json | jq -e '.ok == true' >/dev/null"
run_shell_check 'CHECK-17: historical migration hashes valid' "cd api && \"$API_PYTHON\" -m tools.migration_verifier check --json | jq -e '.findings == []' >/dev/null"
run_shell_check 'CHECK-18: alembic check has no ORM drift' "[ \"\${TASK11_ALLOW_DISPOSABLE_DB:-}\" = 1 ] && printf '%s' \"\${TASK11_DATABASE_URL:-}\" | grep -Eiq '(phase5|task11|scratch|test)' && cd api && DATABASE_URL=\"\$TASK11_DATABASE_URL\" \"$API_PYTHON\" -m alembic check"
run_shell_check 'CHECK-19: clean base-to-head migration' "[ \"\${TASK11_ALLOW_DISPOSABLE_DB:-}\" = 1 ] && printf '%s' \"\${TASK11_SCRATCH_DATABASE_URL:-}\" | grep -Eiq '(phase5|task11|scratch|test)' && cd api && \"$API_PYTHON\" -m tools.migration_verifier check --scratch-url \"\$TASK11_SCRATCH_DATABASE_URL\" --json | jq -e '.ok == true' >/dev/null"
run_shell_check 'CHECK-20: newest downgrade/upgrade' "[ \"\${TASK11_ALLOW_DISPOSABLE_DB:-}\" = 1 ] && printf '%s' \"\${TASK11_DATABASE_URL:-}\" | grep -Eiq '(phase5|task11|scratch|test)' && cd api && DATABASE_URL=\"\$TASK11_DATABASE_URL\" \"$API_PYTHON\" -m alembic downgrade 0007 && DATABASE_URL=\"\$TASK11_DATABASE_URL\" \"$API_PYTHON\" -m alembic upgrade head"
run_test_no_skips 'CHECK-21: database constraints' "$API_PYTHON" -m pytest api/tests_postgresql/test_phase5_schema.py -q

# 22-40. Integrity, recovery, sessions, auth, and audit regression ownership.
run_test_no_skips 'CHECK-22: quick integrity clean fixture' "$API_PYTHON" -m pytest api/tests/test_integrity.py -q
run_test_no_skips 'CHECK-23: full integrity clean fixture' "$API_PYTHON" -m pytest api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-24: missing object detection' "$API_PYTHON" -m pytest api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-25: object metadata mismatch detection' "$API_PYTHON" -m pytest api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-26: orphan object detection' "$API_PYTHON" -m pytest api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-27: repair-safe scope' "$API_PYTHON" -m pytest api/tests/test_integrity.py api/tests/test_storage_integrity.py -q
run_test_no_skips 'CHECK-28: stale import detection' "$API_PYTHON" -m pytest api/tests_phase5/test_import_recovery.py -q
run_test_no_skips 'CHECK-29: safe import recovery' "$API_PYTHON" -m pytest api/tests_phase5/test_import_recovery.py -q
run_test_no_skips 'CHECK-30: active import fencing' "$API_PYTHON" -m pytest api/tests_phase5/test_import_recovery.py -q
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
run_check 'CHECK-51: actionlint passes' actionlint .github/workflows
run_check 'CHECK-52: workflow permissions are least privilege' "$API_PYTHON" scripts/check-workflows.py
run_check 'CHECK-53: all actions use immutable pins' "$API_PYTHON" scripts/check-workflows.py
run_shell_check 'CHECK-54: no privileged pull_request_target execution' '! git grep -n pull_request_target -- .github/workflows'
run_check 'CHECK-55: Dependabot config validates' "$API_PYTHON" scripts/check-workflows.py

# 56-68. Disposable production image/stack checks.
GATE_ENV="$TMPDIR/gate.env"
cp infra/.env.example "$GATE_ENV"
GATE_COMPOSE=(docker compose --project-name "infinityscan-phase5-${BASHPID}" --env-file "$GATE_ENV" -f infra/docker-compose.yml)
run_check 'CHECK-56: production Compose config' "${GATE_COMPOSE[@]}" config
run_check 'CHECK-57: API production image builds' docker build -t infinityscan-phase5-api:gate api
run_check 'CHECK-58: web production image builds' docker build -t infinityscan-phase5-web:gate web
run_shell_check 'CHECK-59: production images contain no tracked secrets' '! git grep -n -E "-----BEGIN .*PRIVATE KEY|postgresql[^ ]*://[^ ]+:[^ ]+@" -- api web'
run_check 'CHECK-60: API image has no real .env' docker run --rm infinityscan-phase5-api:gate sh -c 'test ! -e /app/.env'
run_check 'CHECK-61: production images use non-root users' bash -c 'test "$(docker image inspect infinityscan-phase5-api:gate --format "{{.Config.User}}")" = appuser && test "$(docker image inspect infinityscan-phase5-web:gate --format "{{.Config.User}}")" = nextjs'
if docker info >/dev/null 2>&1; then
  STACK_STARTED=true
  run_check 'CHECK-62: Docker stack starts' "${GATE_COMPOSE[@]}" up -d db migrate api web
  run_check 'CHECK-63: database becomes healthy' "${GATE_COMPOSE[@]}" exec -T db pg_isready
  run_check 'CHECK-64: migrations finish' bash -c 'test "$(docker compose --project-name infinityscan-phase5-'"$BASHPID"' ps -aq migrate | xargs docker inspect --format "{{.State.ExitCode}}")" = 0'
  run_check 'CHECK-65: API health returns 200' curl -fsS http://localhost:8000/health
  run_check 'CHECK-66: frontend returns 200' curl -fsS http://localhost:3000
else
  for check in 'CHECK-62: Docker stack starts' 'CHECK-63: database becomes healthy' 'CHECK-64: migrations finish' 'CHECK-65: API health returns 200' 'CHECK-66: frontend returns 200'; do fail "$check"; done
fi
run_shell_check 'CHECK-67: frontend bundle contains no credentials' '! git grep -n -E "DATABASE_URL|POSTGRES_PASSWORD|S3_SECRET_ACCESS_KEY|JWT_SECRET_KEY" -- web'
if [ "$STACK_STARTED" = true ]; then
  run_shell_check 'CHECK-68: generated logs contain no obvious secrets' '! "${GATE_COMPOSE[@]}" logs 2>/dev/null | grep -Eiq "password=|secret_key=|BEGIN .*PRIVATE KEY"'
else
  fail 'CHECK-68: generated logs contain no obvious secrets'
fi

# 69-70. Final hygiene and cleanup.
run_check 'CHECK-69: git diff --check' git diff --check
if [ "$STACK_STARTED" = true ]; then
  "${GATE_COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  STACK_STARTED=false
fi
run_shell_check 'CHECK-70: disposable cleanup complete' '! docker ps -a --format "{{.Names}}" | grep -E "infinityscan-(dr|phase5)-"'

finish
