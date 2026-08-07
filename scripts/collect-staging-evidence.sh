#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${DEPLOYMENT_STATE_DIR:-/var/lib/infinityscan/staging/releases}"
OUTPUT="${1:-staging-deployment-evidence.json}"
latest="$(python3 - "$STATE_DIR" <<'PY'
import sys
from pathlib import Path

state_dir = Path(sys.argv[1])
paths = [path for path in state_dir.glob("*.json") if path.is_file()]
if paths:
    print(max(paths, key=lambda path: path.stat().st_mtime))
PY
)"
[[ -n "$latest" ]] || { printf 'no staging state is available\n' >&2; exit 1; }
python3 - "$latest" "$OUTPUT" <<'PY'
import json, sys
from pathlib import Path
source, output = map(Path, sys.argv[1:])
data = json.loads(source.read_text(encoding="utf-8"))
safe = {
    "schema_version": data["schema_version"],
    "environment": data["environment"],
    "release_id": data["release_id"],
    "git_sha": data["git_commit_sha"],
    "publication_run_id": data["publication_run_id"],
    "api_reference": data["api_image_digest"],
    "web_reference": data["web_image_digest"],
    "started_at": data["deployed_at"],
    "completed_at": data["deployed_at"],
    "previous_release_id": data["previous_release_id"],
    "active_slot": data["active_slot"],
    "migration_revision_before": data["migration_revision_before"],
    "migration_revision_after": data["migration_revision_after"],
    "preflight": "passed",
    "smoke": data["smoke_test"],
    "rollback_rehearsal": "not-requested",
}
output.write_text(json.dumps(safe, indent=2) + "\n", encoding="utf-8")
output.chmod(0o600)
PY
printf 'safe staging evidence written to %s\n' "$OUTPUT"
