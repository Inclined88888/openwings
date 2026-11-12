#!/usr/bin/env bash
# Upgrade OpenWings in place: back up, pull the new OpenWings revision, rebuild, restart.
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

PULL=1; BACKUP=1; FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="${2:?--from needs a value}"; shift 2 ;;
    --no-pull) PULL=0; shift ;;
    --no-backup) BACKUP=0; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

require_docker
[ -f "$ENV_FILE" ] || die "no .env found; run ./scripts/setup.sh first"
# Settings saved under earlier names are renamed before anything reads them.
migrate_env

before="${FROM:-$(git -C "$WINGS_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}"

if [ "$BACKUP" -eq 1 ] && { [ -n "$(volume_name app-data)" ] || [ -n "$(volume_name "${VOLUME_RENAMES%%:*}")" ]; }; then
  "$WINGS_ROOT/scripts/backup.sh" --name "pre-upgrade-${before}-$(date +%Y%m%d-%H%M%S)"
fi

if [ "$PULL" -eq 1 ]; then
  if git -C "$WINGS_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    if [ -n "$(git -C "$WINGS_ROOT" status --porcelain --untracked-files=no)" ]; then
      die "you have local changes to tracked files; commit or stash them, or re-run with --no-pull"
    fi
    info "Pulling the latest OpenWings revision"
    git -C "$WINGS_ROOT" pull --ff-only
    # Continue with the scripts just pulled, not the copies already in memory.
    after_pull="$(git -C "$WINGS_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    if [ "$after_pull" != "$before" ]; then
      args=(--no-pull --no-backup)
      exec "$WINGS_ROOT/scripts/upgrade.sh" "${args[@]}" --from "$before"
    fi
  else
    warn "not a git checkout; skipping pull"
  fi
fi

after="$(git -C "$WINGS_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# Shared secrets between the app and its helper containers that newer OpenWings
# versions added (the page renderer's, for one). Missing ones are created the
# way setup.sh does; existing values are never changed.
for key in WINGS_BROWSER_TOKEN WINGS_RUNNER_TOKEN WINGS_RENDERER_TOKEN; do
  if [ -z "$(env_get "$key")" ]; then
    env_set "$key" "$(rand_token 36)"
    ok "generated $key"
  fi
done

# The new images are fetched and built while the app keeps running; a failure
# here leaves it running on the previous version.
info "Rebuilding (${before} -> ${after})"
dc pull --ignore-buildable --quiet || die "Downloading the new images failed (see above). OpenWings keeps running on the previous version; fix the cause, then run ./scripts/upgrade.sh --no-pull."
dc build --pull openwings 2>&1 | public_output || die "Building OpenWings failed (see above). It keeps running on the previous version; fix the cause, then run ./scripts/upgrade.sh --no-pull."

# Data volumes from before 1.0.3-w2 are copied to their new names once (the
# app is stopped only for the copy).
migrate_volumes

dc up -d --remove-orphans 2>&1 | public_output || die "Starting OpenWings failed (see above). Fix the cause, then run ./scripts/upgrade.sh --no-pull (your data is kept)."

if wait_healthy openwings 900; then
  ok "Upgrade complete; OpenWings is healthy"
  remove_old_image_tags
else
  explain_unhealthy openwings
  die "OpenWings is not healthy after the upgrade. Roll back: git checkout ${before} && ./scripts/restore.sh <the pre-upgrade backup> --yes"
fi
