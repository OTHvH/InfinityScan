#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
FAKE_HOST=false
for arg in "$@"; do
  case "$arg" in
    --fake-host) FAKE_HOST=true ;;
    *) printf 'usage: %s [--fake-host]\n' "$0" >&2; exit 2 ;;
  esac
done
[[ "$FAKE_HOST" == true ]] || { printf 'the rehearsal requires --fake-host; live hosts are forbidden\n' >&2; exit 2; }
umask 077
workdir="$(mktemp -d "${TMPDIR:-/tmp}/infinityscan-staging-rehearsal.XXXXXX")"
cleanup() { rm -rf -- "$workdir"; }
trap cleanup EXIT

secret_dir="$workdir/secrets"
host_root="$workdir/host"
mkdir -p "$secret_dir" "$host_root/state" "$workdir/release-evidence"
for name in database_url jwt_secret_key csrf_secret_key s3_access_key_id s3_secret_access_key; do
  printf 'synthetic-%s\n' "$name" >"$secret_dir/$name"
  chmod 0600 "$secret_dir/$name"
done
printf 'staging\n' >"$host_root/state/environment.identity"
cat >"$workdir/staging.env" <<EOF
APP_ENV=production
DEPLOYMENT_ENVIRONMENT=staging
APP_DOMAIN=staging.example.test
DATABASE_URL_FILE=$secret_dir/database_url
JWT_SECRET_KEY_FILE=$secret_dir/jwt_secret_key
CSRF_SECRET_KEY_FILE=$secret_dir/csrf_secret_key
S3_ACCESS_KEY_ID_FILE=$secret_dir/s3_access_key_id
S3_SECRET_ACCESS_KEY_FILE=$secret_dir/s3_secret_access_key
S3_ENDPOINT_URL=https://staging-objects.example.test
S3_REGION=auto
S3_BUCKET=synthetic-staging-bucket
BACKUP_BUCKET=synthetic-staging-backup
MEDIA_CSP_ORIGINS=https://staging-objects.example.test
TRUSTED_HOSTS=staging.example.test,api
ALLOWED_ORIGINS=https://staging.example.test
TRUSTED_PROXY_CIDRS=172.31.0.0/24
ALLOW_SYNTHETIC_STAGING_BACKUP_BYPASS=true
EOF

python3 - "$workdir/release-evidence/release-manifest.json" "$workdir/release-evidence/publication-evidence.json" <<'PY'
import json, sys
manifest_path, evidence_path = sys.argv[1:]
digest = "a" * 64
manifest = {
    "schema_version": 1, "release_id": "staging-a", "git_sha": "b" * 40,
    "repository": "example/InfinityScan", "created_at": "2026-08-06T00:00:00Z",
    "lock_hashes": {"api/requirements.txt": "c" * 64, "web/package-lock.json": "d" * 64},
    "images": {
        name: {"repository": f"ghcr.io/example/infinityscan-{name}", "digest": digest,
               "reference": f"ghcr.io/example/infinityscan-{name}@sha256:{digest}",
               "platforms": ["linux/amd64", "linux/arm64"], "dockerfile": f"{name}/Dockerfile"}
        for name in ("api", "web")
    },
}
evidence = {
    "schema_version": 1, "workflow": ".github/workflows/publish-images.yml", "run_id": "123",
    "repository": "example/InfinityScan", "git_sha": "b" * 40, "release_id": "staging-a", "images": {
        name: {"reference": manifest["images"][name]["reference"], "digest": f"sha256:{digest}",
               "platforms": ["linux/amd64", "linux/arm64"], "signature_verified": True,
               "provenance_verified": True, "sbom_file": f"{name}-sbom.spdx.json",
               "scan_file": f"{name}-trivy.json", "scan_passed": True}
        for name in ("api", "web")
    },
}
with open(manifest_path, "w", encoding="utf-8") as stream:
    json.dump(manifest, stream, indent=2)
    stream.write("\n")
with open(evidence_path, "w", encoding="utf-8") as stream:
    json.dump(evidence, stream, indent=2)
    stream.write("\n")
PY
PREFLIGHT_FAKE_HOST=true "$ROOT_DIR/scripts/preflight-staging.sh" \
  --manifest "$workdir/release-evidence/release-manifest.json" \
  --evidence "$workdir/release-evidence/publication-evidence.json" \
  --env-file "$workdir/staging.env" --compose-file "$ROOT_DIR/infra/docker-compose.production.yml" \
  --secret-dir "$secret_dir" --host-root "$host_root"

python3 - "$host_root/state" <<'PY'
import json, sys
from pathlib import Path
state_dir = Path(sys.argv[1])
states = []
def record(release, event, active, smoke, previous=None):
    data = {"schema_version": 1, "environment": "staging", "release_id": release,
            "git_commit_sha": "b" * 40, "api_image_digest": "ghcr.io/example/infinityscan-api@sha256:" + "a" * 64,
            "web_image_digest": "ghcr.io/example/infinityscan-web@sha256:" + "a" * 64,
            "active_slot": active, "smoke_test": smoke, "event": event,
            "previous_release_id": previous, "publication_run_id": "123"}
    path = state_dir / f"{release}-{event}.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    states.append(data)

record("staging-a", "deploy", "blue", "passed")
assert states[-1]["active_slot"] == "blue"
record("staging-b", "deploy", "green", "passed", "staging-a")
assert states[-1]["active_slot"] == "green"
record("staging-c", "failed", "green", "rolled_back", "staging-b")
assert states[-1]["active_slot"] == "green" and states[-1]["event"] == "failed"
record("staging-a", "rollback", "blue", "passed", "staging-b")
assert states[-1]["active_slot"] == "blue" and states[-1]["smoke_test"] == "passed"
print(json.dumps({"release_a": "passed", "release_b": "passed", "release_c": "auto-rolled-back", "manual_rollback": "passed"}))
PY
printf 'local staging rollback rehearsal passed; no live host or cloud service was contacted\n'
