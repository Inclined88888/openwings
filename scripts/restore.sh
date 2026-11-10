#!/usr/bin/env bash
# Restore a backup made by ./scripts/backup.sh. DESTRUCTIVE: replaces the
# current contents of every volume present in the backup.
#
#   ./scripts/restore.sh backups/wings-20260101-030000 --yes
#
# Before touching anything it verifies checksums and archive safety, then takes
# a fresh safety backup of the current state into backups/pre-restore-<time>/
# (skip with --no-safety-backup). Restore with the same .env the backup was
# made with; otherwise stored API keys and passwords cannot be decrypted.
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

SRC=""; YES=0; SAFETY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --no-safety-backup) SAFETY=0; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) [ -z "$SRC" ] || die "only one backup folder may be given"; SRC="$1"; shift ;;
  esac
done

[ -n "$SRC" ] || die "usage: ./scripts/restore.sh <backup-folder> --yes"
[ -d "$SRC" ] || die "no such folder: $SRC"
SRC="$(cd "$SRC" && pwd)"
[ -f "$SRC/odysseus-data.tar.gz" ] || die "$SRC has no odysseus-data.tar.gz; is it a Wings backup?"
[ "$YES" -eq 1 ] || die "restore replaces your current data. Re-run with --yes to confirm."
require_docker
[ -f "$ENV_FILE" ] || die "no .env found. Put back the .env this backup was made with first."

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
  have="$(env_get ODYSSEUS_APP_KEY | sha256_stdin_short)"
  if [ -n "$want" ] && [ "$want" != "$have" ]; then
    warn "ODYSSEUS_APP_KEY in .env differs from the key used when this backup was made."
    warn "Data will restore, but stored API keys and mail/calendar passwords will not decrypt."
    warn "Restore the matching .env first, or re-enter those credentials after restoring."
  fi
fi

IMAGE="$(wings_image)"
helper() { docker run --rm -i --network none --entrypoint sh -v "${SRC}:/backup:ro" "$@"; }

# Upstream's validator rejects absolute paths, '..', links and special files.
helper "$IMAGE" -c "python /app/scripts/odysseus-backup verify /backup/odysseus-data.tar.gz >/dev/null" \
  || die "odysseus-data.tar.gz failed the safety check"
helper "$IMAGE" -c 'python - <<"PY"
import glob, os, tarfile
from pathlib import PurePosixPath
for path in sorted(glob.glob("/backup/*.tar.gz")):
    if path.endswith("/odysseus-data.tar.gz"):
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
if [ "$SAFETY" -eq 1 ] && [ -n "$(volume_name odysseus-data)" ]; then
  info "Taking a safety backup of the current state first"
  "$WINGS_ROOT/scripts/backup.sh" --name "pre-restore-$(date +%Y%m%d-%H%M%S)"
fi

# 3. Restore -----------------------------------------------------------------
info "Stopping the stack"
dc stop >/dev/null

# Make sure every volume exists (fresh host: create without starting the apps).
dc create >/dev/null 2>&1 || true

restore_into() {
  local short="$1" archive="$2" vol
  vol="$(volume_name "$short")"
  if [ -z "$vol" ]; then
    warn "volume ${short} does not exist in this stack; skipping ${archive}"
    return 0
  fi
  helper -v "${vol}:/dst" "$IMAGE" -c "find /dst -mindepth 1 -delete && tar -xzf /backup/${archive} -C /dst --no-same-owner"
  ok "restored ${short}"
}

# odysseus-data: members are data/...; extract only regular files and dirs,
# and skip SQLite -wal/-shm sidecars. The snapshot's .db files come from
# SQLite's backup API and are complete on their own; replaying a live WAL
# captured a moment later onto them can mix two points in time.
vol="$(volume_name odysseus-data)"
[ -n "$vol" ] || die "odysseus-data volume is missing"
helper -v "${vol}:/dst" "$IMAGE" -c 'find /dst -mindepth 1 -delete && python - <<"PY"
import shutil, tarfile
from pathlib import Path, PurePosixPath
dst = Path("/dst")
with tarfile.open("/backup/odysseus-data.tar.gz", "r:gz") as tar:
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
ok "restored odysseus-data"

for f in "$SRC"/*.tar.gz; do
  base="$(basename "$f" .tar.gz)"
  [ "$base" = "odysseus-data" ] && continue
  restore_into "$base" "${base}.tar.gz"
done

# 4. Start ------------------------------------------------------------------
info "Starting the stack (file ownership is repaired on start)"
dc up -d
if wait_healthy odysseus 900; then
  ok "Restore complete and Odysseus is healthy"
else
  dc logs --tail 60 odysseus || true
  die "Odysseus did not become healthy after restore"
fi
