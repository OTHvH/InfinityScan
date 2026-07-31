#!/usr/bin/env bash
# Disposable PostgreSQL + MinIO disaster-recovery drill.
#
# This script refuses production and remote Docker by default, and it only
# destroys its uniquely generated Compose project during cleanup. It requires
# native pg_dump, pg_restore, age, and age-keygen on the host because the drill
# must exercise the same backup tooling used by operators.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ "${APP_ENV:-development}" = "production" ]; then
  printf '%s\n' 'Refusing disaster-recovery drill with APP_ENV=production.' >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Required tool is missing: docker' >&2
  exit 2
fi

if [ "${INFINITYSCAN_DR_ALLOW_REMOTE_DOCKER:-0}" != 1 ]; then
  DOCKER_CONTEXT_NAME="$(docker context show 2>/dev/null || true)"
  DOCKER_CONTEXT_ENDPOINT="$(docker context inspect "$DOCKER_CONTEXT_NAME" --format '{{ (index .Endpoints "docker").Host }}' 2>/dev/null || true)"
  case "$DOCKER_CONTEXT_ENDPOINT" in
    unix:///*) ;;
    *)
      printf '%s\n' 'Refusing DR drill against a non-local Docker context.' >&2
      printf '%s\n' 'Set INFINITYSCAN_DR_ALLOW_REMOTE_DOCKER=1 only to explicitly accept that risk.' >&2
      exit 2
      ;;
  esac
  if [ -n "${DOCKER_HOST:-}" ]; then
    case "$DOCKER_HOST" in
      unix:///*) ;;
      *)
        printf '%s\n' 'Refusing DR drill against a non-local DOCKER_HOST.' >&2
        printf '%s\n' 'Set INFINITYSCAN_DR_ALLOW_REMOTE_DOCKER=1 only to explicitly accept that risk.' >&2
        exit 2
        ;;
    esac
  fi
fi

for tool in jq curl pg_dump pg_restore age age-keygen; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Required tool is missing: %s\n' "$tool" >&2
    exit 2
  fi
done

if ! docker info >/dev/null 2>&1; then
  printf '%s\n' 'Docker daemon is required for the disposable DR drill.' >&2
  exit 2
fi

# shellcheck disable=SC1091 # Runtime path is anchored from the discovered repository root.
. "$REPO_ROOT/scripts/lib/verify-runtime.sh"
verify_runtime_init dr

CHILD_PIDS=()
cleanup() {
  local status=$? pid cleanup_status
  trap - EXIT
  set +e
  for pid in "${CHILD_PIDS[@]}"; do
    kill -TERM "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  done
  verify_runtime_cleanup "$status"
  cleanup_status=$?
  exit "$cleanup_status"
}
trap cleanup EXIT

verify_runtime_preflight

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

TMPDIR="$VERIFY_TMPDIR"
SOURCE_DB_NAME="infinityscan_dr_source_${VERIFY_RUN_ID}"
RESTORE_DB_NAME="infinityscan_dr_restore_${VERIFY_RUN_ID}"
DB_USER="drill_user"
DB_PASSWORD="drill_password_${VERIFY_RUN_ID}"
MINIO_USER="drill_minio"
MINIO_PASSWORD="drill_minio_password_${VERIFY_RUN_ID}"
SOURCE_BUCKET="dr-source-${VERIFY_RUN_ID}"
RESTORE_BUCKET="dr-restore-${VERIFY_RUN_ID}"
DB_PORT=""
MINIO_PORT=""
API_PORT=""
ENV_FILE="$VERIFY_ENV_FILE"
OVERRIDE_FILE="$TMPDIR/compose.override.yml"
RESTORE_OVERRIDE_FILE="$TMPDIR/compose.restore.override.yml"
STATE_DIR="$TMPDIR/state"
BACKUP_DIR="$TMPDIR/backups"
IMPORT_ROOT="$TMPDIR/import-input"
mkdir -p "$STATE_DIR" "$BACKUP_DIR"

verify_runtime_set_env APP_ENV development
verify_runtime_set_env APP_VERSION 0.2.0
verify_runtime_set_env POSTGRES_USER "$DB_USER"
verify_runtime_set_env POSTGRES_PASSWORD "$DB_PASSWORD"
verify_runtime_set_env POSTGRES_DB "$SOURCE_DB_NAME"
verify_runtime_set_env DATABASE_URL "postgresql+psycopg://$DB_USER:$DB_PASSWORD@db:5432/$SOURCE_DB_NAME"
verify_runtime_set_env JWT_SECRET_KEY drill_jwt_secret_key_abcdefghijklmnopqrstuvwxyz_123456
verify_runtime_set_env CSRF_SECRET_KEY drill_csrf_secret_key_abcdefghijklmnopqrstuvwxyz_123456
verify_runtime_set_env COOKIE_SECURE false
verify_runtime_set_env COOKIE_SAME_SITE lax
verify_runtime_set_env TRUSTED_HOSTS localhost,127.0.0.1,testserver,api
verify_runtime_set_env OBJECT_STORAGE_ENABLED true
verify_runtime_set_env S3_ENDPOINT_URL http://minio:9000
verify_runtime_set_env S3_REGION auto
verify_runtime_set_env S3_BUCKET "$SOURCE_BUCKET"
verify_runtime_set_env S3_ACCESS_KEY_ID "$MINIO_USER"
verify_runtime_set_env S3_SECRET_ACCESS_KEY "$MINIO_PASSWORD"
verify_runtime_set_env S3_FORCE_PATH_STYLE true
verify_runtime_set_env MINIO_ROOT_USER "$MINIO_USER"
verify_runtime_set_env MINIO_ROOT_PASSWORD "$MINIO_PASSWORD"
verify_runtime_set_env COPYMANGA_ENABLED false
verify_runtime_set_env LOCAL_CONTENT_ENABLED false
chmod 600 "$ENV_FILE"
verify_runtime_refresh_compose

cat >"$OVERRIDE_FILE" <<EOF
services:
  db:
    ports:
      - "127.0.0.1::5432"
EOF

COMPOSE_BASE=("${VERIFY_COMPOSE[@]}" -f "$OVERRIDE_FILE")
VERIFY_COMPOSE=("${COMPOSE_BASE[@]}")
compose() { "${COMPOSE_BASE[@]}" "$@"; }

stamp_ns() { date +%s%N; }
seconds_between() { "$API_PYTHON" - "$1" "$2" <<'PY'
import sys
print(f"{(int(sys.argv[2]) - int(sys.argv[1])) / 1_000_000_000:.3f}")
PY
}
run_api_container() {
  compose run --rm --no-deps --user "$(id -u):$(id -g)" \
    -v "$REPO_ROOT/scripts/dr_fixture.py:/app/dr_fixture.py:ro" \
    -v "$STATE_DIR:/dr-state" \
    api "$@"
}
run_restore_api_container() {
  "${RESTORE_COMPOSE_BASE[@]}" run --rm --no-deps --user "$(id -u):$(id -g)" \
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
DB_PORT="$(verify_runtime_port db 5432)"
MINIO_PORT="$(verify_runtime_port minio 9000)"
SOURCE_DB_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${DB_PORT}/${SOURCE_DB_NAME}"
RESTORE_DB_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${DB_PORT}/${RESTORE_DB_NAME}"
RESTORE_INTERNAL_DB_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@db:5432/${RESTORE_DB_NAME}"
export APP_ENV=development DATABASE_URL="$SOURCE_DB_URL" BACKUP_DIR S3_ENDPOINT_URL="http://127.0.0.1:${MINIO_PORT}" \
  OBJECT_STORAGE_ENABLED=true S3_ACCESS_KEY_ID="$MINIO_USER" S3_SECRET_ACCESS_KEY="$MINIO_PASSWORD" \
  S3_BUCKET="$SOURCE_BUCKET" S3_REGION=auto S3_FORCE_PATH_STYLE=true
for _ in $(seq 1 60); do
  DB_CONTAINER_ID="$(db_container)"
  if [ -n "$DB_CONTAINER_ID" ] \
    && [ "$(docker inspect "$DB_CONTAINER_ID" --format '{{.State.Health.Status}}' 2>/dev/null || true)" = "healthy" ]; then
    break
  fi
  sleep 1
done
if ! compose exec -T db pg_isready -U "$DB_USER" -d "$SOURCE_DB_NAME" >/dev/null; then
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
printf '%s\n' 'Seeding source database and source bucket with relational data and unique PNG bytes.'
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
DATABASE_URL="$SOURCE_DB_URL" S3_BUCKET="$SOURCE_BUCKET" PYTHON="$API_PYTHON" \
  AGE_RECIPIENT="$AGE_RECIPIENT" scripts/backup-db.sh --objects \
  --allow-test-database --output-dir "$BACKUP_DIR"
BACKUP_END="$(stamp_ns)"
BACKUP_TIME="$(seconds_between "$BACKUP_START" "$BACKUP_END")"
BACKUP_ARTIFACT="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump.age' -print -quit)"
BACKUP_MANIFEST="${BACKUP_ARTIFACT}.manifest.json"
[ -s "$BACKUP_ARTIFACT" ] && [ -s "$BACKUP_MANIFEST" ]
BACKUP_OBJECT_NAME="$(jq -r '.object_artifact.artifact_filename // empty' "$BACKUP_MANIFEST")"
BACKUP_OBJECT_ARTIFACT="$BACKUP_DIR/$BACKUP_OBJECT_NAME"
[ -n "$BACKUP_OBJECT_NAME" ] && [ -s "$BACKUP_OBJECT_ARTIFACT" ]
BASELINE_SHA256="$(jq -r '.object_sha256' "$BASELINE")"
EMBEDDED_SHA256="$(jq -r '.embedded_fixture_sha256' "$BASELINE")"
jq -e --arg object_sha256 "$BASELINE_SHA256" --arg embedded_sha256 "$EMBEDDED_SHA256" '
  .backup_format_version == 2 and .manifest_version == 2 and .encrypted == true and
  .object_artifact.encrypted == true and .object_artifact.object_count == 1 and
  (.object_artifact.objects | length) == 1 and
  .object_artifact.objects[0].sha256 == $object_sha256 and
  .object_artifact.objects[0].sha256 != $embedded_sha256
' "$BACKUP_MANIFEST" >/dev/null
PYTHON="$API_PYTHON" scripts/backup-status.sh --backup-dir "$BACKUP_DIR" --json \
  >"$STATE_DIR/backup-status.json"
jq -e '
  length == 1 and .[0].state == "ok" and .[0].database_state == "ok" and
  .[0].object_state == "ok" and .[0].backup_format_version == 2 and
  .[0].encrypted == true and (.[0].object_artifact | length > 0)
' "$STATE_DIR/backup-status.json" >/dev/null
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
cp "$BACKUP_MANIFEST" "$TMPDIR/truncated.dump.age.manifest.json"
"$API_PYTHON" - "$TMPDIR/truncated.dump.age.manifest.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["artifact_filename"] = "truncated.dump.age"
path.write_text(json.dumps(data))
PY
if DATABASE_URL="$SOURCE_DB_URL" S3_BUCKET="$SOURCE_BUCKET" PYTHON="$API_PYTHON" \
  AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh "$TMPDIR/truncated.dump.age" \
  --target-database "$SOURCE_DB_NAME" --target-bucket "$SOURCE_BUCKET" >/dev/null 2>&1; then
  printf '%s\n' 'truncated backup was accepted' >&2; exit 1
fi
if DATABASE_URL="$SOURCE_DB_URL" S3_BUCKET="$SOURCE_BUCKET" PYTHON="$API_PYTHON" \
  AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh "$BACKUP_ARTIFACT" \
  --manifest "$TMPDIR/missing.manifest.json" --target-database "$SOURCE_DB_NAME" \
  --target-bucket "$SOURCE_BUCKET" >/dev/null 2>&1; then
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
if DATABASE_URL="$SOURCE_DB_URL" S3_BUCKET="$SOURCE_BUCKET" PYTHON="$API_PYTHON" \
  AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh "$BACKUP_ARTIFACT" \
  --manifest "$TMPDIR/modified.manifest.json" --target-database "$SOURCE_DB_NAME" \
  --target-bucket "$SOURCE_BUCKET" >/dev/null 2>&1; then
  printf '%s\n' 'modified checksum was accepted' >&2; exit 1
fi
if DATABASE_URL="$SOURCE_DB_URL" S3_BUCKET="$SOURCE_BUCKET" PYTHON="$API_PYTHON" \
  AGE_IDENTITY_FILE="$WRONG_AGE_IDENTITY" scripts/restore-db.sh "$BACKUP_ARTIFACT" \
  --target-database "$SOURCE_DB_NAME" --target-bucket "$SOURCE_BUCKET" >/dev/null 2>&1; then
  printf '%s\n' 'wrong age identity was accepted' >&2; exit 1
fi
if DATABASE_URL="$SOURCE_DB_URL" S3_BUCKET="$SOURCE_BUCKET" PYTHON="$API_PYTHON" \
  AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh "$BACKUP_ARTIFACT" \
  --target-database "$SOURCE_DB_NAME" --target-bucket "$SOURCE_BUCKET" >/dev/null 2>&1; then
  printf '%s\n' 'non-empty restore was accepted' >&2; exit 1
fi

printf '%s\n' 'Creating distinct empty restore database and restore bucket; source targets remain intact.'
case "$RESTORE_DB_NAME" in infinityscan_dr_restore_*) ;; *) printf '%s\n' 'unsafe restore database name' >&2; exit 2 ;; esac
db_admin psql -h localhost -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"$RESTORE_DB_NAME\""
RESTORE_RELATIONS="$(db_admin psql -h localhost -U "$DB_USER" -d "$RESTORE_DB_NAME" -t -A -v ON_ERROR_STOP=1 \
  -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','m','S','v','f')")"
[ "$RESTORE_RELATIONS" = 0 ]
compose run --rm -e "MINIO_BUCKET=$RESTORE_BUCKET" minio-setup
cat >"$RESTORE_OVERRIDE_FILE" <<EOF
services:
  api:
    environment:
      DATABASE_URL: "$RESTORE_INTERNAL_DB_URL"
      S3_BUCKET: "$RESTORE_BUCKET"
EOF
chmod 600 "$RESTORE_OVERRIDE_FILE"
RESTORE_COMPOSE_BASE=("${COMPOSE_BASE[@]}" -f "$RESTORE_OVERRIDE_FILE")
run_restore_api_container python /app/dr_fixture.py inventory \
  --output /dr-state/restore-bucket-empty.json --prefix ''
jq -e '.object_count == 0 and .objects == []' "$STATE_DIR/restore-bucket-empty.json" >/dev/null

RESTORE_START="$(stamp_ns)"
DATABASE_URL="$RESTORE_DB_URL" S3_BUCKET="$RESTORE_BUCKET" PYTHON="$API_PYTHON" \
  AGE_IDENTITY_FILE="$AGE_IDENTITY" scripts/restore-db.sh "$BACKUP_ARTIFACT" \
  --target-database "$RESTORE_DB_NAME" --target-bucket "$RESTORE_BUCKET"
RESTORE_END="$(stamp_ns)"
RESTORE_TIME="$(seconds_between "$RESTORE_START" "$RESTORE_END")"

EXPECTED_HEAD="$(python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path("api/alembic/migration-manifest.json").read_text())
print(data["migrations"][-1]["revision"])
PY
)"
REVISION="$(db_admin psql -h localhost -U "$DB_USER" -d "$RESTORE_DB_NAME" -t -A -c 'SELECT version_num FROM alembic_version')"
[ "$REVISION" = "$EXPECTED_HEAD" ]
# A deliberately invalid revision must fail schema/head validation.
db_admin psql -h localhost -U "$DB_USER" -d "$RESTORE_DB_NAME" -v ON_ERROR_STOP=1 \
  -c "UPDATE alembic_version SET version_num = '0000'"
if (cd "$REPO_ROOT/api" && DATABASE_URL="$RESTORE_DB_URL" "$API_PYTHON" -m tools.migration_verifier check \
  --require-db-head --json) >/dev/null 2>&1; then
  printf '%s\n' 'invalid restored schema revision was accepted' >&2
  exit 1
fi
db_admin psql -h localhost -U "$DB_USER" -d "$RESTORE_DB_NAME" -v ON_ERROR_STOP=1 \
  -c "UPDATE alembic_version SET version_num = '$EXPECTED_HEAD'"
if ! (cd "$REPO_ROOT/api" && DATABASE_URL="$RESTORE_DB_URL" "$API_PYTHON" -m tools.migration_verifier check \
  --require-db-head --require-schema --json) >"$STATE_DIR/migration-check.json"; then
  cat "$STATE_DIR/migration-check.json" >&2
  exit 1
fi
jq -e '.ok == true' "$STATE_DIR/migration-check.json" >/dev/null
run_restore_api_container python /app/dr_fixture.py validate --baseline /dr-state/baseline.json
run_restore_api_container python /app/dr_fixture.py inventory --output /dr-state/storage-inventory-after.json
jq -e --arg key "$(jq -r '.object_key' "$BASELINE")" --arg sha256 "$BASELINE_SHA256" '
  .object_count == 1 and .objects[0].key == $key and .objects[0].sha256 == $sha256
' "$STATE_DIR/storage-inventory-after.json" >/dev/null
run_api_container python /app/dr_fixture.py inventory --output /dr-state/source-inventory-after-restore.json
cmp -s "$STORAGE_INVENTORY" "$STATE_DIR/source-inventory-after-restore.json"

INTEGRITY_START="$(stamp_ns)"
run_restore_api_container python -m tools.integrity check --mode quick --json | tee "$STATE_DIR/integrity-quick-after.json"
INTEGRITY_END="$(stamp_ns)"
QUICK_INTEGRITY_TIME="$(seconds_between "$INTEGRITY_START" "$INTEGRITY_END")"
run_restore_api_container python -m tools.integrity check --mode full --content-sha256 --json | tee "$STATE_DIR/integrity-full-after.json"
jq -e '.total_issues == 0 and .complete == true' "$STATE_DIR/integrity-quick-after.json" >/dev/null
jq -e '.total_issues == 0 and .complete == true' "$STATE_DIR/integrity-full-after.json" >/dev/null
run_restore_api_container python /app/dr_fixture.py object --baseline /dr-state/baseline.json --orphan
ORPHAN_STATUS=0
run_restore_api_container python -m tools.integrity check --mode full --content-sha256 --json >"$STATE_DIR/integrity-orphan-object.json" 2>"$STATE_DIR/integrity-orphan-object.err" || ORPHAN_STATUS=$?
if [ "$ORPHAN_STATUS" -eq 0 ]; then
  printf '%s\n' 'orphan object was not detected' >&2
  exit 1
fi
run_restore_api_container python /app/dr_fixture.py object --baseline /dr-state/baseline.json --cleanup-orphan
run_restore_api_container python -m tools.integrity check --mode full --content-sha256 --json >"$STATE_DIR/integrity-clean-after-orphan.json"
jq -e '.total_issues == 0 and .complete == true' "$STATE_DIR/integrity-clean-after-orphan.json" >/dev/null

printf '%s\n' 'Starting API against only the restored database and restored bucket.'
"${RESTORE_COMPOSE_BASE[@]}" up -d --force-recreate api
API_PORT="$("${RESTORE_COMPOSE_BASE[@]}" port api 8000 | awk -F: 'NR == 1 {print $NF}')"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS "http://127.0.0.1:${API_PORT}/health" >/dev/null

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
"$API_PYTHON" - "$MEDIA_FILE" "$BASELINE_SHA256" <<'PY'
import hashlib
import sys
from pathlib import Path

payload = Path(sys.argv[1]).read_bytes()
if payload[:8] != b"\x89PNG\r\n\x1a\n":
    raise SystemExit("media redirect did not return the seeded PNG")
if hashlib.sha256(payload).hexdigest() != sys.argv[2]:
    raise SystemExit("restored media bytes do not match the v2 archive manifest")
PY
printf '%s\n' 'restored media redirect verified'

printf '%s\n' 'Launching a real import worker and stopping it with SIGKILL after its first upload.'
PYTHONPATH="$REPO_ROOT/api" "$API_PYTHON" "$REPO_ROOT/scripts/dr_fixture.py" \
  create-import-input --root "$IMPORT_ROOT" --output "$STATE_DIR/import-input.json"
if ! jq -e '.page_count == 2 and (.pages | length) == 2 and .pages[0].sha256 != .pages[1].sha256' \
    "$STATE_DIR/import-input.json" >/dev/null; then
  printf '%s\n' 'SIGKILL import input is invalid.' >&2
  jq '{page_count, pages}' "$STATE_DIR/import-input.json" >&2 || true
  exit 1
fi
printf '%s\n' 'SIGKILL import input created.'

RECOVERY_ENV=(
  env
  APP_ENV=development
  "DATABASE_URL=$RESTORE_DB_URL"
  OBJECT_STORAGE_ENABLED=true
  "S3_ENDPOINT_URL=http://127.0.0.1:${MINIO_PORT}"
  S3_REGION=auto
  "S3_BUCKET=$RESTORE_BUCKET"
  "S3_ACCESS_KEY_ID=$MINIO_USER"
  "S3_SECRET_ACCESS_KEY=$MINIO_PASSWORD"
  S3_FORCE_PATH_STYLE=true
  IMPORT_PENDING_STALE_SECONDS=1
  IMPORT_SCANNING_STALE_SECONDS=1
  IMPORT_UPLOADING_STALE_SECONDS=1
  IMPORT_VERIFYING_STALE_SECONDS=1
  IMPORT_LEASE_SECONDS=8
  IMPORT_RECOVERY_BATCH_SIZE=5
  "PYTHONPATH=$REPO_ROOT/api"
)
CHECKPOINT_MARKER="$STATE_DIR/import-checkpoint.json"
CHILD_STDOUT="$STATE_DIR/import-child.stdout"
CHILD_STDERR="$STATE_DIR/import-child.stderr"
"${RECOVERY_ENV[@]}" "$API_PYTHON" "$REPO_ROOT/scripts/dr_fixture.py" crash-import \
  --root "$IMPORT_ROOT" --marker "$CHECKPOINT_MARKER" >"$CHILD_STDOUT" 2>"$CHILD_STDERR" &
IMPORT_PID=$!
CHILD_PIDS+=("$IMPORT_PID")
MARKER_READY=false
for _ in $(seq 1 120); do
  if [ -s "$CHECKPOINT_MARKER" ] \
    && jq -e '.checkpoint == "after_first_upload" and (.job_id | length) > 0' "$CHECKPOINT_MARKER" >/dev/null 2>&1; then
    MARKER_READY=true
    break
  fi
  if ! kill -0 "$IMPORT_PID" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if [ "$MARKER_READY" != true ]; then
  printf '%s\n' 'import worker did not reach the durable first-upload checkpoint' >&2
  exit 1
fi
MARKER_PID="$(jq -r '.pid' "$CHECKPOINT_MARKER")"
RECOVERY_JOB_ID="$(jq -r '.job_id' "$CHECKPOINT_MARKER")"
if [ "$MARKER_PID" != "$IMPORT_PID" ]; then
  printf 'checkpoint PID %s does not match worker PID %s\n' "$MARKER_PID" "$IMPORT_PID" >&2
  exit 1
fi
printf 'import worker reached durable checkpoint for job %s\n' "$RECOVERY_JOB_ID"
"${RECOVERY_ENV[@]}" "$API_PYTHON" "$REPO_ROOT/scripts/dr_fixture.py" recovery-state \
  --job "$RECOVERY_JOB_ID" --output "$STATE_DIR/import-before-kill.json"
if ! jq -e '
  .job.status == "uploading" and .job.lease_owner_present == true and
  .job.lease_active == true and .job.fencing_token == 1 and
  .job.recovery_attempt_count == 0 and .job_lock_available == false and
  .published_page_count == 0 and (.chapters | length) == 0 and
  (.items | length) == 2 and
  ([.items[] | select(
    .status == "uploading" and .attempt_count == 1 and
    .object.exists == true and .object.verified == true
  )] | length) == 1 and
  ([.items[] | select(
    .status == "pending" and .attempt_count == 0 and .object.exists == false
  )] | length) == 1
' "$STATE_DIR/import-before-kill.json" >/dev/null; then
  printf '%s\n' 'durable pre-kill import state is invalid.' >&2
  jq '.' "$STATE_DIR/import-before-kill.json" >&2 || true
  exit 1
fi
INITIAL_FENCING_TOKEN="$(jq -r '.job.fencing_token' "$STATE_DIR/import-before-kill.json")"
FIRST_OBJECT_ETAG="$(jq -r '.items[] | select(.status == "uploading") | .object.etag // empty' "$STATE_DIR/import-before-kill.json")"
FIRST_OBJECT_SHA="$(jq -r '.items[] | select(.status == "uploading") | .sha256' "$STATE_DIR/import-before-kill.json")"
PENDING_OBJECT_SHA="$(jq -r '.items[] | select(.status == "pending") | .sha256' "$STATE_DIR/import-before-kill.json")"
[ -n "$FIRST_OBJECT_ETAG" ] && [ -n "$FIRST_OBJECT_SHA" ] && [ -n "$PENDING_OBJECT_SHA" ]

kill -KILL "$IMPORT_PID"
set +e
wait "$IMPORT_PID"
KILLED_STATUS=$?
set -e
if [ "$KILLED_STATUS" -ne 137 ]; then
  printf 'SIGKILL worker exit was %s, expected 137\n' "$KILLED_STATUS" >&2
  exit 1
fi
"${RECOVERY_ENV[@]}" "$API_PYTHON" "$REPO_ROOT/scripts/dr_fixture.py" recovery-state \
  --job "$RECOVERY_JOB_ID" --output "$STATE_DIR/import-after-kill.json"
jq -e --argjson fencing "$INITIAL_FENCING_TOKEN" '
  .job.status == "uploading" and .job.lease_owner_present == true and
  .job.lease_active == true and .job.fencing_token == $fencing and
  .job.recovery_attempt_count == 0 and .job_lock_available == true and
  .published_page_count == 0 and (.chapters | length) == 0 and
  ([.items[] | select(.status == "uploading" and .object.verified == true)] | length) == 1 and
  ([.items[] | select(.status == "pending" and .object.exists == false)] | length) == 1
' "$STATE_DIR/import-after-kill.json" >/dev/null

"${RECOVERY_ENV[@]}" "$API_PYTHON" -m tools.import_recovery scan \
  --job "$RECOVERY_JOB_ID" --json >"$STATE_DIR/import-scan-active.json"
jq -e '.jobs_checked == 1 and .active_jobs == 1 and .stale_jobs == 0' \
  "$STATE_DIR/import-scan-active.json" >/dev/null
"${RECOVERY_ENV[@]}" "$API_PYTHON" -m tools.import_recovery recover-safe \
  --job "$RECOVERY_JOB_ID" --json >"$STATE_DIR/import-recover-active.json"
jq -e '
  .attempted == 0 and .recovered == 0 and .skipped_active == 1 and
  (.jobs | length) == 1 and .jobs[0].outcome == "active"
' "$STATE_DIR/import-recover-active.json" >/dev/null
"${RECOVERY_ENV[@]}" "$API_PYTHON" "$REPO_ROOT/scripts/dr_fixture.py" recovery-state \
  --job "$RECOVERY_JOB_ID" --output "$STATE_DIR/import-after-active-skip.json"
jq -e --argjson fencing "$INITIAL_FENCING_TOKEN" '
  .job.status == "uploading" and .job.fencing_token == $fencing and
  .job.recovery_attempt_count == 0 and .published_page_count == 0
' "$STATE_DIR/import-after-active-skip.json" >/dev/null
run_restore_api_container python /app/dr_fixture.py inventory \
  --output /dr-state/inventory-before-import-recovery.json
jq -e '.object_count == 2' "$STATE_DIR/inventory-before-import-recovery.json" >/dev/null

STALE_READY=false
for _ in $(seq 1 80); do
  "${RECOVERY_ENV[@]}" "$API_PYTHON" -m tools.import_recovery scan \
    --job "$RECOVERY_JOB_ID" --json >"$STATE_DIR/import-scan-stale.json"
  if jq -e '.jobs_checked == 1 and .active_jobs == 0 and .stale_jobs == 1' \
    "$STATE_DIR/import-scan-stale.json" >/dev/null 2>&1; then
    STALE_READY=true
    break
  fi
  sleep 0.25
done
if [ "$STALE_READY" != true ]; then
  printf '%s\n' 'killed import did not become stale after its disposable lease expired' >&2
  exit 1
fi
jq -e '
  any(.issues[]; .code == "stuck_uploading" and .recoverable == true) and
  any(.issues[]; .code == "uploaded_before_checkpoint" and .recoverable == true) and
  any(.issues[]; .code == "abandoned_job_item" and .recoverable == true)
' "$STATE_DIR/import-scan-stale.json" >/dev/null
"${RECOVERY_ENV[@]}" "$API_PYTHON" -m tools.import_recovery recover-safe \
  --job "$RECOVERY_JOB_ID" --json >"$STATE_DIR/import-recovered.json"
jq -e '
  .attempted == 1 and .recovered == 1 and .failed_safely == 0 and
  .skipped_active == 0 and .jobs[0].outcome == "succeeded"
' "$STATE_DIR/import-recovered.json" >/dev/null
"${RECOVERY_ENV[@]}" "$API_PYTHON" "$REPO_ROOT/scripts/dr_fixture.py" recovery-state \
  --job "$RECOVERY_JOB_ID" --output "$STATE_DIR/import-final.json"
NEXT_FENCING_TOKEN="$((INITIAL_FENCING_TOKEN + 1))"
jq -e --arg etag "$FIRST_OBJECT_ETAG" --arg first_sha "$FIRST_OBJECT_SHA" \
  --arg pending_sha "$PENDING_OBJECT_SHA" --argjson fencing "$NEXT_FENCING_TOKEN" '
  .job.status == "succeeded" and .job.recovery_attempt_count == 1 and
  .job.fencing_token == $fencing and .job.lease_owner_present == false and
  .job.lease_active == false and .job.uploaded_count == 1 and
  .job.skipped_count == 1 and .job.failed_count == 0 and
  .job_lock_available == true and .published_page_count == 2 and
  (.chapters | length) == 1 and .chapters[0].status == "ready" and
  .chapters[0].page_count == 2 and (.items | length) == 2 and
  ([.items[] | select(
    .sha256 == $first_sha and .status == "skipped" and .attempt_count == 2 and
    .object.verified == true and .object.etag == $etag
  )] | length) == 1 and
  ([.items[] | select(
    .sha256 == $pending_sha and .status == "succeeded" and .attempt_count == 1 and
    .object.verified == true
  )] | length) == 1
' "$STATE_DIR/import-final.json" >/dev/null
run_restore_api_container python /app/dr_fixture.py inventory \
  --output /dr-state/inventory-after-import-recovery.json
jq -e '.object_count == 3' "$STATE_DIR/inventory-after-import-recovery.json" >/dev/null
"$API_PYTHON" - "$STATE_DIR/inventory-before-import-recovery.json" \
  "$STATE_DIR/inventory-after-import-recovery.json" <<'PY'
import json
import sys
from pathlib import Path

before = json.loads(Path(sys.argv[1]).read_text())
after = json.loads(Path(sys.argv[2]).read_text())
after_by_key = {item["key"]: item for item in after["objects"]}
for item in before["objects"]:
    if after_by_key.get(item["key"]) != item:
        raise SystemExit("recovery deleted or changed a pre-existing object")
PY
run_restore_api_container python -m tools.integrity check --mode full \
  --content-sha256 --json >"$STATE_DIR/integrity-full-after-recovery.json"
jq -e '.total_issues == 0 and .complete == true' \
  "$STATE_DIR/integrity-full-after-recovery.json" >/dev/null
run_api_container python /app/dr_fixture.py validate --baseline /dr-state/baseline.json
run_api_container python /app/dr_fixture.py inventory --output /dr-state/source-inventory-final.json
cmp -s "$STORAGE_INVENTORY" "$STATE_DIR/source-inventory-final.json"

printf 'observed backup time: %ss\n' "$BACKUP_TIME"
printf 'backup size: %s bytes\n' "$(stat -c '%s' "$BACKUP_ARTIFACT")"
printf 'object archive size: %s bytes\n' "$(stat -c '%s' "$BACKUP_OBJECT_ARTIFACT")"
printf 'observed restore time: %ss\n' "$RESTORE_TIME"
printf 'integrity verification duration: %ss\n' "$QUICK_INTEGRITY_TIME"
verify_runtime_assert_repo_env_unchanged
printf '%s\n' 'DR drill passed: v2 database/object restore and SIGKILL import recovery preserved complete verified data.'
printf '%s\n' 'These local observed times are not production RTO/RPO guarantees.'
