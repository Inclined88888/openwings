#!/usr/bin/env bash
# Back up every OpenWings data volume into one timestamped folder.
#
#   ./scripts/backup.sh                     -> ./backups/wings-<timestamp>/
#   ./scripts/backup.sh --out /mnt/nas      -> /mnt/nas/wings-<timestamp>/
#   ./scripts/backup.sh --include-models    also archive downloaded models (large)
#
# Safe while the app is running: SQLite databases are copied with SQLite's
# backup API (upstream odysseus-backup), and ChromaDB / Radicale are paused for
# the few seconds their archive takes unless --no-pause is given.
#
# NOT included on purpose: .env. It holds ODYSSEUS_APP_KEY, which decrypts the
# API keys and mail/calendar passwords inside the backup. Keep a copy of .env
# somewhere separate and offline (password manager, encrypted USB). A backup
# without its .env restores everything except stored credentials.
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

OUT_ROOT="${WINGS_ROOT}/backups"; INCLUDE_MODELS=0; PAUSE=1; NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT_ROOT="${2:?--out needs a directory}"; shift 2 ;;
    --name) NAME="${2:?--name needs a value}"; shift 2 ;;
    --include-models) INCLUDE_MODELS=1; shift ;;
    --no-pause) PAUSE=0; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

require_docker
[ -f "$ENV_FILE" ] || die "no .env found; nothing to back up (run ./scripts/setup.sh first)"
[ -n "$(volume_name odysseus-data)" ] || die "the odysseus-data volume does not exist yet"

umask 077
mkdir -p "$OUT_ROOT"
OUT_ROOT="$(cd "$OUT_ROOT" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="${OUT_ROOT}/${NAME:-wings-${STAMP}}"
[ -e "$DEST" ] && die "$DEST already exists"
mkdir -p "$DEST"
HOST_UID="$(id -u)"; HOST_GID="$(id -g)"

# One-off helper container from the OpenWings image itself (it has python, tar
# and sha256sum), with the backup folder mounted at /backup.
IMAGE="$(wings_image)"
DATA_VOL="$(volume_name odysseus-data)"
helper() {
  docker run --rm -i --network none --entrypoint sh -v "${DEST}:/backup:z" "$@"
}

info "Backing up to ${DEST}"

# 1. App data: database, settings, notes, tasks, memory, documents, uploads,
#    skills, research reports, mail attachment cache. Mounted read-write on
#    purpose: SQLite needs the -shm file to read a WAL database consistently.
helper -v "${DATA_VOL}:/app/data" "$IMAGE" -c "umask 077 && python /app/scripts/odysseus-backup snapshot --out /backup/odysseus-data.tar.gz --include-research --include-attachments >/backup/odysseus-data.json && chown ${HOST_UID}:${HOST_GID} /backup/odysseus-data.tar.gz /backup/odysseus-data.json"
ok "odysseus-data"

# 2. Plain-file volumes, archived with tar from a read-only mount.
archive_volume() {
  local short="$1" svc="${2:-}" vol was_running=0 cid
  vol="$(volume_name "$short")"
  if [ -z "$vol" ]; then
    return 0
  fi
  if [ -n "$svc" ] && [ "$PAUSE" -eq 1 ]; then
    cid="$(container_id "$svc")"
    if [ -n "$cid" ] && [ "$(docker inspect --format '{{.State.Running}}' "$cid")" = "true" ]; then
      was_running=1
      dc stop "$svc" >/dev/null
    fi
  fi
  helper -v "${vol}:/src:ro" "$IMAGE" -c "umask 077 && tar -czf /backup/${short}.tar.gz -C /src . && chown ${HOST_UID}:${HOST_GID} /backup/${short}.tar.gz" \
    || { [ "$was_running" -eq 1 ] && dc start "$svc" >/dev/null; die "failed to archive ${short}"; }
  if [ "$was_running" -eq 1 ]; then
    dc start "$svc" >/dev/null
  fi
  ok "$short"
}

archive_volume odysseus-ssh
archive_volume agent-workspaces runner
archive_volume chromadb-data chromadb
archive_volume radicale-data radicale
archive_volume caddy-data
if [ "$INCLUDE_MODELS" -eq 1 ]; then
  archive_volume ollama-models ollama
  archive_volume odysseus-hf-cache
fi

# 3. The CalDAV account file lives outside the volumes.
if [ -f "$WINGS_ROOT/config/radicale/users" ]; then
  cp "$WINGS_ROOT/config/radicale/users" "$DEST/radicale-users"
  chmod 600 "$DEST/radicale-users"
  ok "radicale accounts"
fi

# 4. Manifest with versions and checksums.
{
  echo "wings_backup_version=1"
  echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "compose_project=$(project_name)"
  echo "image=$(cd "$WINGS_ROOT" && docker compose config --images 2>/dev/null | grep -m1 '^openwings:' || true)"
  echo "wings_git_commit=$(git -C "$WINGS_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "app_key_fingerprint=$(env_get ODYSSEUS_APP_KEY | sha256_stdin_short)"
} > "${DEST}/MANIFEST" 2>/dev/null || true
(
  cd "$DEST"
  for f in *.tar.gz radicale-users; do
    [ -e "$f" ] || continue
    sum="$(sha256_file "$f")" || exit 1
    [ -n "$sum" ] || exit 1
    printf '%s  %s\n' "$sum" "$f"
  done
) > "${DEST}/SHA256SUMS" || die "could not checksum the backup files (are they readable by $(id -un)?)"

ok "Backup complete: ${DEST}"
du -sh "$DEST" 2>/dev/null | awk '{print "     size: " $1}'
warn "This backup does not contain .env. Keep a separate copy of .env, or the stored API keys and passwords cannot be decrypted after a restore."
