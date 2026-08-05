#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
DEPLOY_USER="infinityscan"
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --user=*) DEPLOY_USER="${arg#*=}" ;;
    *) printf 'usage: %s [--dry-run] [--user=name]\n' "$0" >&2; exit 2 ;;
  esac
done

[[ "$(uname -s)" == "Linux" ]] || { printf 'Linux is required\n' >&2; exit 1; }
if [[ "$EUID" -ne 0 && "$DRY_RUN" != true ]]; then
  printf 'root is required for host bootstrap\n' >&2
  exit 1
fi

dirs=(
  /opt/infinityscan
  /opt/infinityscan/releases
  /var/lib/infinityscan
  /var/lib/infinityscan/releases
  /var/log/infinityscan
  /etc/infinityscan
  /etc/infinityscan/secrets
)

for dir in "${dirs[@]}"; do
  printf '%s %s\n' "$(if "$DRY_RUN"; then printf 'would-create'; else printf 'create'; fi)" "$dir"
  if [[ "$DRY_RUN" != true ]]; then
    install -d -m 0750 "$dir"
  fi
done

if [[ "$DRY_RUN" != true ]]; then
  if ! getent group "$DEPLOY_USER" >/dev/null; then
    groupadd --system "$DEPLOY_USER"
  fi
  if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    useradd --system --gid "$DEPLOY_USER" --home-dir /opt/infinityscan --shell /usr/sbin/nologin "$DEPLOY_USER"
  fi
  chown -R "$DEPLOY_USER:$DEPLOY_USER" /opt/infinityscan /var/lib/infinityscan /var/log/infinityscan
  chown root:"$DEPLOY_USER" /etc/infinityscan /etc/infinityscan/secrets
  chmod 0750 /etc/infinityscan /etc/infinityscan/secrets
fi

printf 'host bootstrap %s\n' "$(if "$DRY_RUN"; then printf 'dry-run complete'; else printf 'complete'; fi)"
