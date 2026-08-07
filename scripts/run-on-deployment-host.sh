#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
ACTION="${1:-}"
shift || true
case "$ACTION" in
  preflight|deploy|smoke|rollback|status|collect-safe-evidence|install-evidence) ;;
  *) printf 'allowed actions: preflight deploy smoke rollback status collect-safe-evidence install-evidence\n' >&2; exit 2 ;;
esac

transport="${DEPLOY_HOST_TRANSPORT:-local}"
if [[ "$transport" == local ]]; then
  case "$ACTION" in
    preflight) exec "$ROOT_DIR/scripts/preflight-staging.sh" "$@" ;;
    deploy) exec "$ROOT_DIR/scripts/deploy-production.sh" --environment staging "$@" ;;
    smoke) exec env SMOKE_MODE=staging "$ROOT_DIR/scripts/smoke-deployment.sh" "$@" ;;
    rollback) exec "$ROOT_DIR/scripts/rollback-production.sh" "$@" ;;
    status) exec python3 "$ROOT_DIR/scripts/staging-status.py" "$@" ;;
    collect-safe-evidence) exec "$ROOT_DIR/scripts/collect-staging-evidence.sh" "$@" ;;
    install-evidence)
      [[ $# -eq 2 ]] || { printf 'install-evidence requires manifest and evidence paths\n' >&2; exit 2; }
      destination="${DEPLOYMENT_REMOTE_EVIDENCE_DIR:-${DEPLOYMENT_HOST_ROOT:-/opt/infinityscan}/staging-evidence}"
      mkdir -p "$destination"
      cp -- "$1" "$destination/release-manifest.json"
      cp -- "$2" "$destination/publication-evidence.json"
      chmod 0640 "$destination"/*.json
      ;;
  esac
fi

[[ "$transport" == ssh ]] || { printf 'DEPLOY_HOST_TRANSPORT must be local or ssh\n' >&2; exit 1; }
host="${DEPLOYMENT_HOST:?DEPLOYMENT_HOST is required for SSH transport}"
user="${DEPLOYMENT_USER:-infinityscan}"
known_hosts="${DEPLOYMENT_KNOWN_HOSTS_FILE:?DEPLOYMENT_KNOWN_HOSTS_FILE is required}"
key_file="${DEPLOYMENT_SSH_KEY_FILE:?DEPLOYMENT_SSH_KEY_FILE is required}"
[[ -f "$known_hosts" && -f "$key_file" ]] || { printf 'SSH trust files are missing\n' >&2; exit 1; }
case "$ACTION" in
  preflight) remote=(preflight-staging.sh "$@") ;;
  deploy) remote=(deploy-production.sh --environment staging "$@") ;;
  smoke) remote=(smoke-deployment.sh "$@") ;;
  rollback) remote=(rollback-production.sh "$@") ;;
  status) remote=(staging-status.py "$@") ;;
  collect-safe-evidence) remote=(collect-staging-evidence.sh "$@") ;;
  install-evidence) remote=(install-evidence "$@") ;;
esac
if [[ "$ACTION" == install-evidence ]]; then
  [[ $# -eq 2 ]] || { printf 'install-evidence requires manifest and evidence paths\n' >&2; exit 2; }
  remote_dir="${DEPLOYMENT_REMOTE_EVIDENCE_DIR:?DEPLOYMENT_REMOTE_EVIDENCE_DIR is required}"
  scp -i "$key_file" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$known_hosts" "$1" "$2" "$user@$host:$remote_dir/"
  exit 0
fi
printf -v remote_command '%q ' "${remote[@]}"
exec ssh -i "$key_file" -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$known_hosts" "$user@$host" -- "$remote_command"
