#!/usr/bin/env bash
# Restore a backup made by ./scripts/backup.sh. DESTRUCTIVE: replaces the
# current contents of every volume present in the backup.
#
#   ./scripts/restore.sh backups/wings-20260101-030000 --yes
#
# Before touching anything it verifies checksums and archive safety, then takes
# a fresh safety backup of the current state into backups/pre-restore-<time>/
# (skip with --no-safety-backup). Restore with the same .env the backup was
# made with. If its key differs, the script stops before changing anything;
# --accept-key-loss restores anyway and starts the app with the stored API
# keys and mail/calendar passwords unreadable (re-enter them in Settings).
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

SRC=""; YES=0; SAFETY=1; ACCEPT_KEY_LOSS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --no-safety-backup) SAFETY=0; shift ;;
    --accept-key-loss) ACCEPT_KEY_LOSS=1; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) [ -z "$SRC" ] || die "only one backup folder may be given"; SRC="$1"; shift ;;
  esac
done

[ -n "$SRC" ] || die "usage: ./scripts/restore.sh <backup-folder> --yes"
[ -d "$SRC" ] || die "no such folder: $SRC"
SRC="$(cd "$SRC" && pwd)"
# Backups made before 1.0.3-w2 name the app data archive (and two volume
# archives) after the earlier volume names; both are restored the same way.
OLD_DATA="ody""sseus-data"
if [ -f "$SRC/app-data.tar.gz" ]; then DATA_ARCHIVE="app-data.tar.gz"
elif [ -f "$SRC/${OLD_DATA}.tar.gz" ]; then DATA_ARCHIVE="${OLD_DATA}.tar.gz"
else die "$SRC has no app-data.tar.gz; is it an OpenWings backup?"; fi
[ "$YES" -eq 1 ] || die "restore replaces your current data. Re-run with --yes to confirm."
require_docker
[ -f "$ENV_FILE" ] || die "no .env found. Put back the .env this backup was made with first."
migrate_env

# 1. Integrity --------------------------------------------------------------
if [ -f "$SRC/SHA256SUMS" ]; then
  while read -r sum file; do
    [ -f "$SRC/$file" ] || die "missing $file listed in SHA256SUMS"
    [ "$(sha256_file "$SRC/$file")" = "$sum" ] || die "checksum mismatch: $file"
  done < "$SRC/SHA256SUMS"
  ok "checksums match"
else
  warn "no SHA256SUMS in backup; skipping checksum verification"
fi

if [ -f "$SRC/MANIFEST" ]; then
  want="$(sed -n 's/^app_key_fingerprint=//p' "$SRC/MANIFEST")"
  have="$(setting APP_KEY | sha256_stdin_short)"
  if [ -n "$want" ] && [ "$want" != "$have" ]; then
    if [ "$ACCEPT_KEY_LOSS" -ne 1 ]; then
      die "OPENWINGS_APP_KEY in .env differs from the key this backup was made with. Put back the matching .env and re-run (nothing was changed), or re-run with --accept-key-loss to restore everything except stored credentials."
    fi
    warn "Restoring with a different key: stored API keys and mail/calendar passwords will be unreadable; re-enter them in Settings."
    env_set WINGS_ACCEPT_KEY_LOSS 1
  fi
fi

IMAGE="$(wings_image)"
helper() { docker run --rm -i --network none --entrypoint sh -v "${SRC}:/backup:ro,z" "$@"; }

# The app's own validator rejects absolute paths, '..', links and special files.
if ! helper "$IMAGE" -c "t=/app/scripts/openwings-backup && { [ -e \$t ] || t=/app/scripts/ody''sseus-backup; } && python \$t verify /backup/${DATA_ARCHIVE} >/dev/null" 2>&1 | public_output >&2; then
  die "${DATA_ARCHIVE} failed the safety check"
fi
helper -e DATA_ARCHIVE="$DATA_ARCHIVE" "$IMAGE" -c 'python - <<"PY"
import glob, os, tarfile
from pathlib import PurePosixPath
for path in sorted(glob.glob("/backup/*.tar.gz")):
    if os.path.basename(path) == os.environ["DATA_ARCHIVE"]:
        continue
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            p = PurePosixPath(m.name)
            if p.is_absolute() or ".." in p.parts:
                raise SystemExit("%s: unsafe path %r" % (os.path.basename(path), m.name))
            if m.issym() or m.islnk():
                t = PurePosixPath(m.linkname)
                if t.is_absolute() or ".." in t.parts:
                    raise SystemExit("%s: unsafe link %r -> %r" % (os.path.basename(path), m.name, m.linkname))
            if m.ischr() or m.isblk() or m.isfifo():
                raise SystemExit("%s: device or fifo entry %r" % (os.path.basename(path), m.name))
PY' || die "a volume archive failed the safety check"
ok "archives are safe to extract"

# 2. Safety backup of the current state -------------------------------------
if [ "$SAFETY" -eq 1 ] && { [ -n "$(volume_name app-data)" ] || [ -n "$(volume_name "$OLD_DATA")" ]; }; then
  info "Taking a safety backup of the current state first"
  "$WINGS_ROOT/scripts/backup.sh" --name "pre-restore-$(date +%Y%m%d-%H%M%S)"
fi

# 3. Restore -----------------------------------------------------------------
info "Stopping the stack"
dc stop >/dev/null

# The CalDAV account file lives outside the volumes; put it back first so
# Docker never creates a directory in its place.
if [ -f "$SRC/radicale-users" ]; then
  mkdir -p "$WINGS_ROOT/config/radicale"
  cat "$SRC/radicale-users" > "$WINGS_ROOT/config/radicale/users"
  chmod 644 "$WINGS_ROOT/config/radicale/users"
  ok "restored radicale accounts"
fi

# Data from before 1.0.3-w2 moves to the current volume names first; the
# restore then replaces it there (so no new, empty volume is created next to
# an earlier one).
migrate_volumes
# Make sure every volume exists (fresh host: create without starting the apps).
dc create >/dev/null 2>&1 || true

restore_into() {
  local short="$1" archive="$2" vol
  vol="$(volume_name "$short")"
  if [ -z "$vol" ]; then
    warn "volume ${short} does not exist in this stack; skipping ${archive}"
    return 0
  fi
  # Keep the numeric owners recorded in the archive (Radicale runs as 2999,
  # the app as PUID); the archives were made as root, so they hold them.
  helper -v "${vol}:/dst" "$IMAGE" -c "find /dst -mindepth 1 -delete && tar --numeric-owner -xzpf /backup/${archive} -C /dst"
  ok "restored ${short}"
}

# App data: members are data/...; extract only regular files and dirs,
# and skip SQLite -wal/-shm sidecars. The snapshot's .db files come from
# SQLite's backup API and are complete on their own; replaying a live WAL
# captured a moment later onto them can mix two points in time.
vol="$(volume_name app-data)"
[ -n "$vol" ] || die "the app data volume is missing"
helper -v "${vol}:/dst" -e DATA_ARCHIVE="$DATA_ARCHIVE" "$IMAGE" -c 'find /dst -mindepth 1 -delete && python - <<"PY"
import shutil, tarfile
from pathlib import Path, PurePosixPath
dst = Path("/dst")
import os
with tarfile.open("/backup/" + os.environ["DATA_ARCHIVE"], "r:gz") as tar:
    for m in tar.getmembers():
        parts = PurePosixPath(m.name).parts
        if not parts or parts[0] != "data" or ".." in parts or m.name.startswith("/"):
            raise SystemExit("unsafe member: %r" % m.name)
        rel = parts[1:]
        if not rel or m.name.endswith(("-wal", "-shm", "-journal")):
            continue
        target = dst.joinpath(*rel)
        if m.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif m.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
PY'
ok "restored app data"

for f in "$SRC"/*.tar.gz; do
  base="$(basename "$f" .tar.gz)"
  [ "${base}.tar.gz" = "$DATA_ARCHIVE" ] && continue
  short="$base"
  case "$base" in
    "ody""sseus-ssh") short="app-ssh" ;;
    "ody""sseus-hf-cache") short="hf-cache" ;;
  esac
  restore_into "$short" "${base}.tar.gz"
done

# 4. Start ------------------------------------------------------------------
info "Starting the stack (file ownership is repaired on start)"
dc up -d --remove-orphans
if wait_healthy openwings 900; then
  ok "Restore complete and OpenWings is healthy"
else
  dc logs --tail 60 openwings || true
  die "OpenWings did not become healthy after restore"
fi
