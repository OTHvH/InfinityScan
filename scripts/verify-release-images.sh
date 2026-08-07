#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
MANIFEST=""
EXPECTED_REPOSITORY=""
EXPECTED_WORKFLOW_IDENTITY=""
EXPECTED_GIT_SHA=""
SBOM_DIR=""
SCAN_SUMMARY=""
OFFLINE=false

usage() {
  printf 'usage: %s --manifest FILE --expected-repository OWNER/REPO --expected-workflow-identity ID [--expected-git-sha SHA] [--sbom-dir DIR] [--scan-summary FILE] [--offline]\n' "$0" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    --expected-repository) EXPECTED_REPOSITORY="$2"; shift 2 ;;
    --expected-workflow-identity) EXPECTED_WORKFLOW_IDENTITY="$2"; shift 2 ;;
    --expected-git-sha) EXPECTED_GIT_SHA="$2"; shift 2 ;;
    --sbom-dir) SBOM_DIR="$2"; shift 2 ;;
    --scan-summary) SCAN_SUMMARY="$2"; shift 2 ;;
    --offline) OFFLINE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

[[ -n "$MANIFEST" && -n "$EXPECTED_REPOSITORY" ]] || { usage; exit 2; }
python3 "$ROOT_DIR/scripts/validate-release-manifest.py" "$MANIFEST" >/dev/null

manifest_values="$(python3 - "$MANIFEST" "$EXPECTED_REPOSITORY" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
expected = sys.argv[2]
if manifest["repository"].lower() != expected.lower():
    raise SystemExit("manifest repository does not match the expected repository")
expected_owner = expected.split("/", 1)[0].lower()
print(manifest["git_sha"])
for component in ("api", "web"):
    image = manifest["images"][component]
    if not image["repository"].startswith(f"ghcr.io/{expected_owner}/"):
        raise SystemExit(f"{component} repository is outside the expected owner")
    print(f"{component}\t{image['reference']}\t{image['digest']}")
PY
)"
if [[ -z "$EXPECTED_GIT_SHA" ]]; then
  EXPECTED_GIT_SHA="$(printf '%s\n' "$manifest_values" | awk 'NR == 1 {print}')"
fi
[[ "$EXPECTED_GIT_SHA" =~ ^[0-9a-f]{40}$ ]] || { printf 'invalid expected Git SHA\n' >&2; exit 1; }

if [[ -n "$SBOM_DIR" ]]; then
  for component in api web; do
    [[ -s "$SBOM_DIR/$component-sbom.spdx.json" ]] || {
      printf 'missing SBOM for %s\n' "$component" >&2
      exit 1
    }
  done
fi
if [[ -n "$SCAN_SUMMARY" ]]; then
  [[ -s "$SCAN_SUMMARY" ]] || { printf 'missing scan summary\n' >&2; exit 1; }
  grep -Eq '"passed"[[:space:]]*:[[:space:]]*true' "$SCAN_SUMMARY" || {
    printf 'scan summary is not passing\n' >&2
    exit 1
  }
fi

if [[ "$OFFLINE" == true ]]; then
  printf 'offline release image verification passed for %s\n' "$MANIFEST"
  exit 0
fi

command -v docker >/dev/null 2>&1 || { printf 'docker is required\n' >&2; exit 1; }
command -v cosign >/dev/null 2>&1 || { printf 'cosign is required\n' >&2; exit 1; }
command -v gh >/dev/null 2>&1 || { printf 'gh is required\n' >&2; exit 1; }
expected_owner="${EXPECTED_REPOSITORY%%/*}"
expected_owner="${expected_owner,,}"

while IFS=$'\t' read -r component reference digest; do
  [[ "$component" == api || "$component" == web ]] || continue
  [[ "$reference" == ghcr.io/${expected_owner}/*@${digest} ]] || {
    printf '%s reference has the wrong repository or digest\n' "$component" >&2
    exit 1
  }
  inspect="$(docker buildx imagetools inspect "$reference")"
  [[ "$inspect" == *"linux/amd64"* && "$inspect" == *"linux/arm64"* ]] || {
    printf '%s image is not multi-architecture\n' "$component" >&2
    exit 1
  }
  cosign verify \
    --certificate-identity "$EXPECTED_WORKFLOW_IDENTITY" \
    --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
    "$reference" >/dev/null
  attestation_file="${TMPDIR:-/tmp}/infinityscan-${component}-attestation.json"
  gh attestation verify "oci://$reference" --repo "$EXPECTED_REPOSITORY" --format json >"$attestation_file"
  grep -Fq "$EXPECTED_GIT_SHA" "$attestation_file" || {
    printf '%s provenance does not contain the expected Git SHA\n' "$component" >&2
    exit 1
  }
done < <(printf '%s\n' "$manifest_values" | tail -n +2)

printf 'release image verification passed for %s\n' "$MANIFEST"
