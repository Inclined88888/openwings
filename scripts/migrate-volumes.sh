#!/usr/bin/env bash
# Move OpenWings data to the volume names used since 1.0.3-w2.
#
#   ./scripts/migrate-volumes.sh               copy any earlier data volume to its new name
#                                              (setup.sh and upgrade.sh do this for you)
#   ./scripts/migrate-volumes.sh --recopy      a new volume was created empty before the
#                                              copy: set it aside (kept) and copy again;
#                                              also copies again when the new volumes
#                                              were deleted after an earlier copy
#   ./scripts/migrate-volumes.sh --remove-old  delete the earlier volumes, once the copies
#                                              exist and OpenWings works with them (keeps
#                                              any that changed after they were copied)
#
# The copy never changes the earlier volumes; they are the way back until you
# remove them. Take a backup first if you have none (./scripts/backup.sh).
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

MODE=copy
while [ $# -gt 0 ]; do
  case "$1" in
    --remove-old) MODE=remove; shift ;;
    --recopy) MODE=recopy; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

require_docker
[ -f "$ENV_FILE" ] || die "no .env found; run ./scripts/setup.sh first"
migrate_env

case "$MODE" in
  copy)
    migrate_volumes
    ok "Data volumes are up to date. Start OpenWings with: docker compose up -d"
    exit 0 ;;
  recopy)
    recopy_volumes
    ok "Data volumes are up to date. Start OpenWings with: docker compose up -d"
    exit 0 ;;
esac

# --remove-old: only an earlier volume whose data a finished copy holds is
# removed, and only when it has not changed since (same file count, bytes and
# newest change time as when it was copied; an older OpenWings started on it
# afterwards would have changed it). Any other is kept and named.
removed=0; kept=0
for pair in $VOLUME_RENAMES; do
  oldv="$(volume_name "${pair%%:*}")"; newv="$(volume_name "${pair##*:}")"
  [ -n "$oldv" ] || continue
  [ -n "$newv" ] || die "the new ${pair##*:} volume does not exist yet; run ./scripts/setup.sh (or this script without --remove-old) first. Nothing was removed."
  if [ -n "$(docker ps -aq --filter "volume=${oldv}")" ]; then
    die "an old container still uses an earlier volume; run ./scripts/setup.sh first so OpenWings uses the new ones. Nothing was removed."
  fi
done
img=""
for pair in $VOLUME_RENAMES; do
  oldv="$(volume_name "${pair%%:*}")"; newv="$(volume_name "${pair##*:}")"
  [ -n "$oldv" ] || continue
  [ -n "$img" ] || img="$(_mv_image)" || die "could not find or build the OpenWings image; nothing was removed"
  reason=""
  if ! volume_made_from "$newv" "$oldv" "$img"; then
    reason="${newv} was not made from it by a finished copy"
  elif [ "$(volume_label "$newv" "$SKIPPED_LABEL")" != "$oldv" ]; then
    record="$(_mv_state "$newv" "$img")"
    then_stats="$(printf '%s\n' "$record" | sed -n 3p)"
    now_stats="$(docker run --rm --network none --entrypoint sh -v "${oldv}:/from:ro" "$img" -c "${MV_STATS_SH}; mv_stats /from")" || now_stats="unreadable"
    if [ "$(printf '%s\n' "$record" | sed -n 2p)" != "$oldv" ] || [ -z "$then_stats" ]; then
      reason="${newv} has no record of the copy (a restore replaces it), so it cannot be compared"
    elif [ "$then_stats" != "$now_stats" ]; then
      reason="it changed after it was copied (files: ${then_stats%% *} then, ${now_stats%% *} now), so it may hold data the copy lacks"
    fi
  fi
  if [ -n "$reason" ]; then
    warn "kept ${oldv}: ${reason}."
    warn "  To copy it again: ./scripts/migrate-volumes.sh --recopy. If you are sure you do not need it: docker volume rm ${oldv}"
    kept=$((kept + 1))
    continue
  fi
  docker volume rm "$oldv" >/dev/null
  removed=$((removed + 1))
done
ok "Removed ${removed} earlier data volume(s)."
[ "$kept" -eq 0 ] || warn "Kept ${kept} earlier volume(s); see above."
