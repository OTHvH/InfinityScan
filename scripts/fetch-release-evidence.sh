#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
REPOSITORY=""
RUN_ID=""
ARTIFACT_NAME=""
EXPECTED_SHA=""
DESTINATION=""
FIXTURE_DIR=""

usage() {
  printf 'usage: %s --repository OWNER/REPO --run-id ID --artifact NAME --expected-git-sha SHA --destination DIR [--fixture-dir DIR]\n' "$0" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repository) REPOSITORY="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --artifact) ARTIFACT_NAME="$2"; shift 2 ;;
    --expected-git-sha) EXPECTED_SHA="$2"; shift 2 ;;
    --destination) DESTINATION="$2"; shift 2 ;;
    --fixture-dir) FIXTURE_DIR="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

[[ "$REPOSITORY" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] || { usage; exit 2; }
[[ "$RUN_ID" =~ ^[0-9]+$ && "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ && -n "$ARTIFACT_NAME" && -n "$DESTINATION" ]] || { usage; exit 2; }
umask 077
rm -rf -- "$DESTINATION"
mkdir -p -- "$DESTINATION"

if [[ -n "$FIXTURE_DIR" ]]; then
  [[ -d "$FIXTURE_DIR" ]] || { printf 'fixture directory is missing\n' >&2; exit 1; }
  cp -a -- "$FIXTURE_DIR"/. "$DESTINATION"/
else
  command -v gh >/dev/null 2>&1 || { printf 'gh is required outside fixture mode\n' >&2; exit 1; }
  run_json="$(gh api "repos/$REPOSITORY/actions/runs/$RUN_ID")"
  RUN_JSON="$run_json" REPOSITORY="$REPOSITORY" EXPECTED_SHA="$EXPECTED_SHA" python3 - <<'PY'
import json, os, sys
run = json.loads(os.environ["RUN_JSON"])
if run.get("name") != "Publish Immutable Multi-Architecture Images":
    raise SystemExit("publication input is not an image publication run")
if run.get("path") != ".github/workflows/publish-images.yml":
    raise SystemExit("publication input used an unexpected workflow")
if run.get("conclusion") != "success":
    raise SystemExit("publication input did not succeed")
if run.get("head_repository", {}).get("full_name") != os.environ["REPOSITORY"]:
    raise SystemExit("publication input belongs to another repository")
if run.get("head_sha") != os.environ["EXPECTED_SHA"]:
    raise SystemExit("publication run SHA does not match the requested SHA")
PY
  gh run download "$RUN_ID" --repo "$REPOSITORY" --name "$ARTIFACT_NAME" --dir "$DESTINATION"
fi

find "$DESTINATION" -type l -print -quit | grep -q . && { printf 'release evidence may not contain symlinks\n' >&2; exit 1; } || true
find "$DESTINATION" -type f -perm /111 -print -quit | grep -q . && { printf 'release evidence may not contain executable files\n' >&2; exit 1; } || true
[[ -s "$DESTINATION/release-manifest.json" && -s "$DESTINATION/publication-evidence.json" ]] || {
  printf 'release evidence is missing the manifest or publication evidence\n' >&2
  exit 1
}
if [[ -s "$DESTINATION/sbom-checksums.txt" ]]; then
  (cd "$DESTINATION" && sha256sum -c sbom-checksums.txt >/dev/null)
else
  printf 'release evidence is missing SBOM checksums\n' >&2
  exit 1
fi
python3 "$ROOT_DIR/scripts/validate-staging-evidence.py" \
  --manifest "$DESTINATION/release-manifest.json" \
  --evidence "$DESTINATION/publication-evidence.json" \
  --repository "$REPOSITORY" \
  --git-sha "$EXPECTED_SHA"
printf 'release evidence fetched into %s\n' "$DESTINATION"
