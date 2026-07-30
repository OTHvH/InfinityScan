#!/usr/bin/env bash
# Disposable PostgreSQL + MinIO disaster-recovery drill.
#
# This script refuses production mode and only destroys a uniquely generated
# database name inside its own Compose project. It requires native pg_dump,
# pg_restore, age, and age-keygen on the host because the drill must exercise
# the same backup tooling used by operators.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ "${APP_ENV:-development}" = "production" ]; then
  printf '%s\n' 'Refusing disaster-recovery drill with APP_ENV=production.' >&2
  exit 2
fi
if [ -e "$REPO_ROOT/infra/.env" ]; then
  printf '%s\n' 'Refusing DR drill while infra/.env exists; use only its generated synthetic environment.' >&2
  exit 2
fi

for tool in docker jq curl pg_dump pg_restore age age-keygen; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Required tool is missing: %s\n' "$tool" >&2
    exit 2
  fi
done

if ! docker info >/dev/null 2>&1; then
  printf '%s\n' 'Docker daemon is required for the disposable DR drill.' >&2
  exit 2
fi

if [ -n "${PYTHON:-}" ]; then
  API_PYTHON="$PYTHON"
elif [ -x "$REPO_ROOT/api/.venv/bin/python" ]; then
  API_PYTHON="$REPO_ROOT/api/.venv/bin/python"
elif [ -x /tmp/infinityscan-audit-venv/bin/python ]; then
  API_PYTHON=/tmp/infinityscan-audit-venv/bin/python
else
  API_PYTHON="$(command -v python3 || true)"
fi
if [ ! -x "$API_PYTHON" ]; then
  printf 'Python with project dependencies is required: %s\n' "$API_PYTHON" >&2
  exit 2
fi

TMPDIR="$(mktemp -d /tmp/infinityscan-dr-XXXXXX)"
PROJECT="infinityscan-dr-${BASHPID}"
DB_NAME="infinityscan_dr_${BASHPID}"
DB_USER="drill_user"
DB_PASSWORD="drill_password_${BASHPID}"
MINIO_USER="drill_minio"
MINIO_PASSWORD="drill_minio_password_${BASHPID}"
DB_PORT="$((54000 + BASHPID % 1000))"
MINIO_PORT="$((55000 + BASHPID % 1000))"
API_PORT="$((56000 + BASHPID % 1000))"
ENV_FILE="$TMPDIR/compose.env"
OVERRIDE_FILE="$TMPDIR/compose.override.yml"
STATE_DIR="$TMPDIR/state"
BACKUP_DIR="$TMPDIR/backups"
INFRA_ENV_FILE="$REPO_ROOT/infra/.env"
INFRA_ENV_CREATED=false
mkdir -p "$STATE_DIR" "$BACKUP_DIR"

cleanup() {
  local status=$?
  set +e
  if [ -n "${COMPOSE_BASE+x}" ]; then
    "${COMPOSE_BASE[@]}" --profile storage down -v --remove-orphans >/dev/null 2>&1
  fi
  if [ "$INFRA_ENV_CREATED" = true ]; then
    rm -f "$INFRA_ENV_FILE"
  fi
  rm -rf "$TMPDIR"
  exit "$status"
}
trap cleanup EXIT

cat >"$ENV_FILE" <<EOF
APP_ENV=development
APP_VERSION=0.2.0
POSTGRES_USER=$DB_USER
POSTGRES_PASSWORD=$DB_PASSWORD
POSTGRES_DB=$DB_NAME
DATABASE_URL=postgresql+psycopg://$DB_USER:$DB_PASSWORD@db:5432/$DB_NAME
JWT_SECRET_KEY=drill_jwt_secret_key_abcdefghijklmnopqrstuvwxyz_123456
CSRF_SECRET_KEY=drill_csrf_secret_key_abcdefghijklmnopqrstuvwxyz_123456
COOKIE_SECURE=false
COOKIE_SAME_SITE=lax
TRUSTED_HOSTS=localhost,127.0.0.1,testserver,api
OBJECT_STORAGE_ENABLED=true
S3_ENDPOINT_URL=http://minio:9000
S3_REGION=auto
S3_BUCKET=drill-pages
S3_ACCESS_KEY_ID=$MINIO_USER
S3_SECRET_ACCESS_KEY=$MINIO_PASSWORD
S3_FORCE_PATH_STYLE=true
MINIO_ROOT_USER=$MINIO_USER
MINIO_ROOT_PASSWORD=$MINIO_PASSWORD
COPYMANGA_ENABLED=false
LOCAL_CONTENT_ENABLED=false
EOF
chmod 600 "$ENV_FILE"
cp "$ENV_FILE" "$INFRA_ENV_FILE"
chmod 600 "$INFRA_ENV_FILE"
INFRA_ENV_CREATED=true

cat >"$OVERRIDE_FILE" <<EOF
services:
  db:
    ports:
      - "127.0.0.1:${DB_PORT}:5432"
  minio:
    ports:
      - "127.0.0.1:${MINIO_PORT}:9000"
  api:
    ports:
      - "127.0.0.1:${API_PORT}:8000"
EOF

COMPOSE_BASE=(docker compose --project-name "$PROJECT" --env-file "$ENV_FILE" -f "$REPO_ROOT/infra/docker-compose.yml" -f "$OVERRIDE_FILE")
compose() { "${COMPOSE_BASE[@]}" "$@"; }
DB_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${DB_PORT}/${DB_NAME}"
export APP_ENV=development DATABASE_URL="$DB_URL" BACKUP_DIR S3_ENDPOINT_URL="http://127.0.0.1:${MINIO_PORT}" \
  OBJECT_STORAGE_ENABLED=true S3_ACCESS_KEY_ID="$MINIO_USER" S3_SECRET_ACCESS_KEY="$MINIO_PASSWORD" \
  S3_BUCKET=drill-pages S3_REGION=auto S3_FORCE_PATH_STYLE=true

stamp_ns() { date +%s%N; }
seconds_between() { "$API_PYTHON" - "$1" "$2" <<'PY'
import sys
print(f"{(int(sys.argv[2]) - int(sys.argv[1])) / 1_000_000_000:.3f}")
PY
}
run_api_container() {
  compose run --rm --no-deps \
    -v "$REPO_ROOT/scripts/dr_fixture.py:/app/dr_fixture.py:ro" \
    -v "$STATE_DIR:/dr-state" \
    api "$@"
}
db_container() { compose ps -q db | tr -d '[:space:]'; }
db_admin() {
  docker exec -e "PGPASSWORD=$DB_PASSWORD" "$(db_container)" "$@"
}

printf '%s\n' 'Starting disposable PostgreSQL and MinIO services.'
compose up -d db minio
for _ in $(seq 1 60); do
  DB_CONTAINER_ID="$(db_container)"
  if [ -n "$DB_CONTAINER_ID" ] \
    && [ "$(docker inspect "$DB_CONTAINER_ID" --format '{{.State.Health.Status}}' 2>/dev/null || true)" = "healthy" ]; then
    break
  fi
  sleep 1
done
if ! compose exec -T db pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null; then
  compose logs db >&2 || true
  exit 1
fi
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${MINIO_PORT}/minio/health/live" >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! curl -fsS "http://127.0.0.1:${MINIO_PORT}/minio/health/live" >/dev/null; then
  compose logs minio >&2 || true
  exit 1
fi
compose run --rm minio-setup

compose up -d migrate
MIGRATE_CONTAINER="$(compose ps -aq migrate | tr -d '[:space:]')"
for _ in $(seq 1 60); do
  [ -n "$MIGRATE_CONTAINER" ] && [ "$(docker inspect "$MIGRATE_CONTAINER" --format '{{.State.Status}}' 2>/dev/null || true)" = "exited" ] && break
  sleep 1
done
MIGRATE_EXIT="$(docker inspect "$MIGRATE_CONTAINER" --format '{{.State.ExitCode}}' 2>/dev/null || true)"
if [ "$MIGRATE_EXIT" != 0 ]; then
  compose logs migrate >&2 || true
  exit 1
fi
compose up -d api
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS "http://127.0.0.1:${API_PORT}/health" >/dev/null

printf '%s\n' 'Seeding synthetic relational data and actual tiny PNG objects.'
run_api_container python /app/dr_fixture.py seed --output /dr-state/baseline.json
BASELINE="$STATE_DIR/baseline.json"
[ -s "$BASELINE" ]
run_api_container python /app/dr_fixture.py inventory --output /dr-state/storage-inventory.json
STORAGE_INVENTORY="$STATE_DIR/storage-inventory.json"
[ -s "$STORAGE_INVENTORY" ]
jq -e '.object_count == 1 and (.objects | length) == 1' "$STORAGE_INVENTORY" >/dev/null

INTEGRITY_START="$(stamp_ns)"
run_api_container python -m tools.integrity check --mode quick --json | tee "$STATE_DIR/integrity-quick-before.json"
INTEGRITY_END="$(stamp_ns)"
printf 'initial quick integrity duration=%ss\n' "$(seconds_between "$INTEGRITY_START" "$INTEGRITY_END")"

AGE_IDENTITY="$TMPDIR/age-identity.txt"
WRONG_AGE_IDENTITY="$TMPDIR/wrong-age-identity.txt"
AGE_KEYGEN_OUTPUT="$TMPDIR/age-keygen.txt"
age-keygen -o "$AGE_IDENTITY" >"$AGE_KEYGEN_OUTPUT" 2>/dev/null
age-keygen -o "$WRONG_AGE_IDENTITY" >/dev/null 2>&1
chmod 600 "$AGE_IDENTITY"
AGE_RECIPIENT="$(awk 'tolower($0) ~ /public key/ {print $NF}' "$AGE_IDENTITY" | tail -n 1)"
if [ -z "$AGE_RECIPIENT" ]; then
  AGE_RECIPIENT="$(awk 'tolower($0) ~ /public key/ {print $NF}' "$AGE_KEYGEN_OUTPUT" | tail -n 1)"
fi
[ -n "$AGE_RECIPIENT" ]

BACKUP_START="$(stamp_ns)"
PYTHON="$API_PYTHON" AGE_RECIPIENT="$AGE_RECIPIENT" scripts/backup-db.sh \
  --allow-test-database --output-dir "$BACKUP_DIR"
BACKUP_END="$(stamp_ns)"
BACKUP_TIME="$(seconds_between "$BACKUP_START" "$BACKUP_END")"
BACKUP_ARTIFACT="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump.age' -print -quit)"
BACKUP_MANIFEST="${BACKUP_ARTIFACT}.manifest.json"
[ -s "$BACKUP_ARTIFACT" ] && [ -s "$BACKUP_MANIFEST" ]
cp "$STORAGE_INVENTORY" "$STATE_DIR/storage-inventory-manifest.json"
"$API_PYTHON" - "$BACKUP_MANIFEST" <<'PY'
import json
import re
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
for key, value in manifest.items():
    if re.search(r"database[_-]?url|dsn|password|secret|credential|private[_-]?key|identity|token|presign", str(key), re.I):
        raise SystemExit(f"forbidden metadata key: {key}")
    if re.search(r"postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s]+:[^\s]+@|-----BEGIN [^-]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}", json.dumps(value), re.I):
        raise SystemExit("secret-bearing metadata value")
PY
# Prove negative restore safety before the real restore: checksum mutation,
# missing manifest, and truncated artifacts must be rejected without a DB call.
cp "$BACKUP_ARTIFACT" "$TMPDIR/truncated.dump.age"
truncate -s -1 "$TMPDIR/truncated.dump.age"
if PYTHON="$API_PYTHON" AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh "$TMPDIR/truncated.dump.age" --target-database "$DB_NAME" >/dev/null 2>&1; then
  printf '%s\n' 'truncated backup was accepted' >&2; exit 1
fi
if PYTHON="$API_PYTHON" AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh "$BACKUP_ARTIFACT" --manifest "$TMPDIR/missing.manifest.json" --target-database "$DB_NAME" >/dev/null 2>&1; then
  printf '%s\n' 'missing manifest was accepted' >&2; exit 1
fi
cp "$BACKUP_MANIFEST" "$TMPDIR/modified.manifest.json"
"$API_PYTHON" - "$TMPDIR/modified.manifest.json" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["dump_sha256"] = "0" * 64
path.write_text(json.dumps(data))
PY
if PYTHON="$API_PYTHON" AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh "$BACKUP_ARTIFACT" --manifest "$TMPDIR/modified.manifest.json" --target-database "$DB_NAME" >/dev/null 2>&1; then
  printf '%s\n' 'modified checksum was accepted' >&2; exit 1
fi
if PYTHON="$API_PYTHON" AGE_IDENTITY_FILE="$WRONG_AGE_IDENTITY" scripts/restore-db.sh "$BACKUP_ARTIFACT" --target-database "$DB_NAME" >/dev/null 2>&1; then
  printf '%s\n' 'wrong age identity was accepted' >&2; exit 1
fi
if PYTHON="$API_PYTHON" AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh "$BACKUP_ARTIFACT" --target-database "$DB_NAME" >/dev/null 2>&1; then
  printf '%s\n' 'non-empty restore was accepted' >&2; exit 1
fi

printf '%s\n' 'Destroying and recreating only the explicitly generated disposable database.'
case "$DB_NAME" in infinityscan_dr_*) ;; *) printf '%s\n' 'unsafe database name' >&2; exit 2 ;; esac
db_admin psql -h localhost -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE \"$DB_NAME\""
db_admin psql -h localhost -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"$DB_NAME\""

RESTORE_START="$(stamp_ns)"
PYTHON="$API_PYTHON" AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh \
  "$BACKUP_ARTIFACT" --target-database "$DB_NAME"
RESTORE_END="$(stamp_ns)"
RESTORE_TIME="$(seconds_between "$RESTORE_START" "$RESTORE_END")"

EXPECTED_HEAD="$(python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path("api/alembic/migration-manifest.json").read_text())
print(data["migrations"][-1]["revision"])
PY
)"
REVISION="$(db_admin psql -h localhost -U "$DB_USER" -d "$DB_NAME" -t -A -c 'SELECT version_num FROM alembic_version')"
[ "$REVISION" = "$EXPECTED_HEAD" ]
# A deliberately invalid revision must fail schema/head validation.
db_admin psql -h localhost -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
  -c "UPDATE alembic_version SET version_num = '0000'"
if (cd "$REPO_ROOT/api" && DATABASE_URL="$DB_URL" "$API_PYTHON" -m tools.migration_verifier check \
  --database-url "$DB_URL" --require-db-head --json) >/dev/null 2>&1; then
  printf '%s\n' 'invalid restored schema revision was accepted' >&2
  exit 1
fi
db_admin psql -h localhost -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
  -c "UPDATE alembic_version SET version_num = '$EXPECTED_HEAD'"
if ! (cd "$REPO_ROOT/api" && DATABASE_URL="$DB_URL" "$API_PYTHON" -m tools.migration_verifier check \
  --database-url "$DB_URL" --require-db-head --require-schema --json) >"$STATE_DIR/migration-check.json"; then
  cat "$STATE_DIR/migration-check.json" >&2
  exit 1
fi
jq -e '.ok == true' "$STATE_DIR/migration-check.json" >/dev/null
run_api_container python /app/dr_fixture.py validate --baseline /dr-state/baseline.json
run_api_container python /app/dr_fixture.py inventory --output /dr-state/storage-inventory-after.json
cmp -s "$STORAGE_INVENTORY" "$STATE_DIR/storage-inventory-after.json"

INTEGRITY_START="$(stamp_ns)"
run_api_container python -m tools.integrity check --mode quick --json | tee "$STATE_DIR/integrity-quick-after.json"
INTEGRITY_END="$(stamp_ns)"
QUICK_INTEGRITY_TIME="$(seconds_between "$INTEGRITY_START" "$INTEGRITY_END")"
run_api_container python -m tools.integrity check --mode full --content-sha256 --json | tee "$STATE_DIR/integrity-full-after.json"
jq -e '.total_issues == 0 and .complete == true' "$STATE_DIR/integrity-quick-after.json" >/dev/null
jq -e '.total_issues == 0 and .complete == true' "$STATE_DIR/integrity-full-after.json" >/dev/null
run_api_container python /app/dr_fixture.py object --baseline /dr-state/baseline.json --orphan
ORPHAN_STATUS=0
run_api_container python -m tools.integrity check --mode full --content-sha256 --json >"$STATE_DIR/integrity-orphan-object.json" 2>"$STATE_DIR/integrity-orphan-object.err" || ORPHAN_STATUS=$?
if [ "$ORPHAN_STATUS" -eq 0 ]; then
  printf '%s\n' 'orphan object was not detected' >&2
  exit 1
fi
run_api_container python /app/dr_fixture.py object --baseline /dr-state/baseline.json --cleanup-orphan
run_api_container python -m tools.integrity check --mode full --content-sha256 --json >"$STATE_DIR/integrity-clean-after-orphan.json"
run_api_container python /app/dr_fixture.py object --baseline /dr-state/baseline.json
MISSING_OBJECT_STATUS=0
run_api_container python -m tools.integrity check --mode quick --json >"$STATE_DIR/integrity-missing-object.json" 2>"$STATE_DIR/integrity-missing-object.err" || MISSING_OBJECT_STATUS=$?
printf 'missing-object integrity exit=%s\n' "$MISSING_OBJECT_STATUS"
if [ "$MISSING_OBJECT_STATUS" -eq 0 ]; then
  printf '%s\n' 'missing object was not detected' >&2; exit 1
fi
if ! run_api_container python /app/dr_fixture.py object --baseline /dr-state/baseline.json --restore; then
  printf '%s\n' 'synthetic object restore failed' >&2
  exit 1
fi
printf '%s\n' 'synthetic object restored after negative test'
printf '%s\n' 'checking integrity after synthetic object restoration'
if ! run_api_container python -m tools.integrity check --mode quick --json >"$STATE_DIR/integrity-object-restored.json" 2>"$STATE_DIR/integrity-object-restored.err"; then
  cat "$STATE_DIR/integrity-object-restored.err" >&2
  cat "$STATE_DIR/integrity-object-restored.json" >&2
  exit 1
fi

printf '%s\n' 'checking restored API authentication and user state'
COOKIE_JAR="$TMPDIR/drill.cookies"
CSRF_JSON="$TMPDIR/drill-csrf.json"
LOGIN_JSON="$TMPDIR/drill-login.json"
CSRF_STATUS="$(curl -sS -c "$COOKIE_JAR" -o "$CSRF_JSON" -w '%{http_code}' "http://127.0.0.1:${API_PORT}/auth/csrf")"
CSRF="$(jq -r '.csrf_token // empty' "$CSRF_JSON")"
printf 'csrf endpoint status=%s fields=%s token_length=%s\n' "$CSRF_STATUS" "$(jq -c 'keys' "$CSRF_JSON" 2>/dev/null || printf invalid)" "${#CSRF}"
[ "$CSRF_STATUS" = 200 ] && [ -n "$CSRF" ]
LOGIN_STATUS="$(curl -sS -b "$COOKIE_JAR" -c "$COOKIE_JAR" -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' -d '{"username":"drill_user","password":"Drill-only-password-42"}' \
  -o "$LOGIN_JSON" -w '%{http_code}' "http://127.0.0.1:${API_PORT}/auth/login")"
printf 'login endpoint status=%s\n' "$LOGIN_STATUS"
[ "$LOGIN_STATUS" = 200 ]
jq -e '.user.username == "drill_user"' "$LOGIN_JSON" >/dev/null
printf '%s\n' 'restored login verified'
BOOKMARKS_JSON="$TMPDIR/bookmarks.json"
curl -fsS -b "$COOKIE_JAR" "http://127.0.0.1:${API_PORT}/bookmarks" -o "$BOOKMARKS_JSON"
jq -e 'length == 1 and .[0].series_path_word == "drill-series"' "$BOOKMARKS_JSON" >/dev/null
printf '%s\n' 'restored bookmark ownership verified'
CHAPTER_ID="$(jq -r '.chapter_id' "$BASELINE")"
PROGRESS_JSON="$TMPDIR/progress.json"
curl -fsS -b "$COOKIE_JAR" "http://127.0.0.1:${API_PORT}/progress/drill-series/${CHAPTER_ID}" -o "$PROGRESS_JSON"
jq -e '.last_page == 1 and .scroll_position == 0.5' "$PROGRESS_JSON" >/dev/null
printf '%s\n' 'restored reading progress verified'
PAGE_ID="$(jq -r '.page_id' "$BASELINE")"
MEDIA_HEADERS="$TMPDIR/media.headers"
curl -fsS -D "$MEDIA_HEADERS" -o /dev/null "http://127.0.0.1:${API_PORT}/media/pages/${PAGE_ID}"
MEDIA_LOCATION="$(awk 'tolower($1) == "location:" {sub(/^[^:]+:[[:space:]]*/, ""); print; exit}' "$MEDIA_HEADERS" | tr -d '\r')"
printf '%s' "$MEDIA_LOCATION" | grep -Eiq '^https?://'
MEDIA_FILE="$TMPDIR/restored-page.png"
curl -fsSL --connect-to "minio:9000:127.0.0.1:${MINIO_PORT}" "$MEDIA_LOCATION" -o "$MEDIA_FILE"
"$API_PYTHON" - "$MEDIA_FILE" <<'PY'
import sys
from pathlib import Path

if Path(sys.argv[1]).read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
    raise SystemExit("media redirect did not return the seeded PNG")
PY
printf '%s\n' 'restored media redirect verified'

printf 'observed backup time: %ss\n' "$BACKUP_TIME"
printf 'backup size: %s bytes\n' "$(stat -c '%s' "$BACKUP_ARTIFACT")"
printf 'observed restore time: %ss\n' "$RESTORE_TIME"
printf 'integrity verification duration: %ss\n' "$QUICK_INTEGRITY_TIME"
printf '%s\n' 'DR drill passed: database, object storage, authentication, bookmarks, progress, media, and audit events survived restore.'
printf '%s\n' 'These local observed times are not production RTO/RPO guarantees.'
