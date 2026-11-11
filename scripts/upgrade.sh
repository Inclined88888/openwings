#!/usr/bin/env bash
# Upgrade Wings in place: back up, pull the new Wings revision, rebuild, restart.
#
#   ./scripts/upgrade.sh               backup + git pull + rebuild + restart
#   ./scripts/upgrade.sh --no-pull     rebuild from the files you already have
#   ./scripts/upgrade.sh --no-backup   skip the pre-upgrade backup (not advised)
#
# Data lives in named volumes, so rebuilding or recreating containers never
# touches it. The pre-upgrade backup is your rollback: if the new version
# misbehaves, `git checkout <previous commit>` and ./scripts/restore.sh it.
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

PULL=1; BACKUP=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-pull) PULL=0; shift ;;
    --no-backup) BACKUP=0; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

require_docker
[ -f "$ENV_FILE" ] || die "no .env found; run ./scripts/setup.sh first"

before="$(git -C "$WINGS_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

if [ "$BACKUP" -eq 1 ] && [ -n "$(volume_name odysseus-data)" ]; then
  "$WINGS_ROOT/scripts/backup.sh" --name "pre-upgrade-${before}-$(date +%Y%m%d-%H%M%S)"
fi

if [ "$PULL" -eq 1 ]; then
  if git -C "$WINGS_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    if [ -n "$(git -C "$WINGS_ROOT" status --porcelain --untracked-files=no)" ]; then
      die "you have local changes to tracked files; commit or stash them, or re-run with --no-pull"
    fi
    info "Pulling the latest Wings revision"
    git -C "$WINGS_ROOT" pull --ff-only
  else
    warn "not a git checkout; skipping pull"
  fi
fi

after="$(git -C "$WINGS_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# Shared secrets between the app and its helper containers that newer Wings
# versions added (the page renderer's, for one). Missing ones are created the
# way setup.sh does; existing values are never changed.
for key in WINGS_BROWSER_TOKEN WINGS_RUNNER_TOKEN WINGS_RENDERER_TOKEN; do
  if [ -z "$(env_get "$key")" ]; then
    env_set "$key" "$(rand_token 36)"
    ok "generated $key"
  fi
done

info "Rebuilding (${before} -> ${after})"
dc pull --ignore-buildable --quiet
dc build --pull odysseus
dc up -d --remove-orphans

if wait_healthy odysseus 900; then
  ok "Upgrade complete; Odysseus is healthy"
else
  dc logs --tail 80 odysseus || true
  die "Odysseus is not healthy after the upgrade. Roll back: git checkout ${before} && ./scripts/restore.sh <the pre-upgrade backup> --yes"
fi
