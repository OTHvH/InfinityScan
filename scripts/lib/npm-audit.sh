#!/usr/bin/env bash
# Shared npm security policy for release gates.
set -uo pipefail

PROJECT_DIR="${1:-}"
EXCEPTIONS_FILE="${2:-}"
REPORT_FILE="${3:-}"

if [ -z "$PROJECT_DIR" ] || [ -z "$EXCEPTIONS_FILE" ] || [ -z "$REPORT_FILE" ]; then
  printf 'usage: %s PROJECT_DIR EXCEPTIONS_FILE REPORT_FILE\n' "$0" >&2
  exit 2
fi
if ! command -v npm >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
  printf 'npm and jq are required for dependency auditing\n' >&2
  exit 2
fi
if [ ! -d "$PROJECT_DIR" ] || [ ! -f "$EXCEPTIONS_FILE" ]; then
  printf 'audit project or exception file does not exist\n' >&2
  exit 2
fi

STDERR_FILE="${REPORT_FILE}.stderr"
EXIT_CODE_FILE="${REPORT_FILE}.exit-code"
(cd "$PROJECT_DIR" && npm audit --include=dev --json >"$REPORT_FILE" 2>"$STDERR_FILE")
NPM_EXIT_CODE=$?
printf '%s\n' "$NPM_EXIT_CODE" >"$EXIT_CODE_FILE"
printf 'npm audit exit code: %s\n' "$NPM_EXIT_CODE"

if [ ! -s "$REPORT_FILE" ] || ! jq -e '
  .auditReportVersion == 2 and
  (.vulnerabilities | type == "object") and
  (.metadata.vulnerabilities | type == "object") and
  (.metadata.vulnerabilities.total | type == "number")
' "$REPORT_FILE" >/dev/null 2>&1; then
  printf 'npm audit returned malformed or empty JSON\n' >&2
  if [ -s "$STDERR_FILE" ]; then
    tail -n 10 "$STDERR_FILE" >&2
  fi
  exit 1
fi

TODAY="$(date -u +%F)"
if ! jq -e --arg today "$TODAY" '
  (.exceptions | type == "array") and
  all(.exceptions[];
    (.advisory_id | type == "string") and
    (.package | type == "string" and length > 0) and
    .severity == "moderate" and
    (.justification | type == "string" and length > 0) and
    (.execution_context | type == "string" and length > 0) and
    (.review_date | type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}$")) and
    .review_date >= $today
  )
' "$EXCEPTIONS_FILE" >/dev/null 2>&1; then
  printf 'npm audit exception file is malformed, expired, or exempts a non-moderate finding\n' >&2
  exit 1
fi

TOTAL="$(jq -r '.metadata.vulnerabilities.total' "$REPORT_FILE")"
ACTUAL_TOTAL="$(jq -r '.vulnerabilities | length' "$REPORT_FILE")"
if { [ "$NPM_EXIT_CODE" -eq 0 ] && [ "$TOTAL" -ne 0 ]; } || \
   { [ "$NPM_EXIT_CODE" -ne 0 ] && [ "$TOTAL" -eq 0 ]; }; then
  printf 'npm audit exit code and vulnerability metadata disagree\n' >&2
  exit 1
fi
if [ "$TOTAL" -ne "$ACTUAL_TOTAL" ]; then
  printf 'npm audit vulnerability metadata count does not match the report\n' >&2
  exit 1
fi

if [ "$TOTAL" -gt 0 ]; then
  jq -r '
    def fix_description:
      if . == false then "false"
      elif . == true then "true"
      else "object(name=" + (.name // "unknown") +
           ", version=" + (.version // "unknown") +
           ", breaking=" + ((.isSemVerMajor // false) | tostring) + ")"
      end;
    .vulnerabilities | to_entries[] |
    .key as $package | .value as $finding |
    "package: \($package)",
    "severity: \($finding.severity)",
    "direct: \($finding.isDirect)",
    "vulnerable range: \($finding.range)",
    "installed paths: \(($finding.nodes // []) | join(", "))",
    "affected parents: \(($finding.effects // []) | join(", "))",
    "fix availability: \($finding.fixAvailable | fix_description)",
    ($finding.via[]? | select(type == "object") |
      "advisory: \(.source) | \(.url // "URL unavailable") | range \(.range // "unknown")"),
    ($finding.via[]? | select(type == "string") | "dependency path via: \(.)"),
    "---"
  ' "$REPORT_FILE"
fi

HIGH_OR_CRITICAL="$(jq '[.vulnerabilities[] | select(.severity == "high" or .severity == "critical")] | length' "$REPORT_FILE")"
if [ "$HIGH_OR_CRITICAL" -gt 0 ]; then
  printf 'npm audit policy rejects all unresolved high and critical vulnerabilities\n' >&2
  exit 1
fi

if ! jq -e --slurpfile policy "$EXCEPTIONS_FILE" '
  $policy[0].exceptions as $exceptions |
  [
    .vulnerabilities | to_entries[] as $entry |
    $entry.value.via[]? |
    select(type == "object") |
    {
      advisory_id: (.source | tostring),
      package: (.name // $entry.key),
      severity: .severity
    }
  ] as $advisories |
  def excepted($advisory):
    any($exceptions[];
      .advisory_id == $advisory.advisory_id and
      .package == $advisory.package and
      .severity == $advisory.severity
    );
  def package_excepted($package):
    any($advisories[]; .package == $package and excepted(.));
  all(
    .vulnerabilities | to_entries[] | select(.value.severity == "moderate");
    . as $finding |
    ($finding.value.via | length) > 0 and
    all($finding.value.via[];
      if type == "object" then
        excepted({
          advisory_id: (.source | tostring),
          package: (.name // $finding.key),
          severity: .severity
        })
      elif type == "string" then
        package_excepted(.)
      else
        false
      end
    )
  )
' "$REPORT_FILE" >/dev/null; then
  printf 'npm audit policy rejects an unresolved moderate vulnerability without an exact documented exception\n' >&2
  exit 1
fi

printf 'npm audit policy passed: %s known vulnerabilities\n' "$TOTAL"
