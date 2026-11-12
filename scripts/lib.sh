# shellcheck shell=bash
# Shared helpers for the OpenWings scripts. Sourced, not executed.
# Portable to the bash 3.2 that ships with macOS: no associative arrays,
# no ${var,,}, no mapfile.

WINGS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${WINGS_ROOT}/.env"
# Memory to give Docker. Colima's default 2 GB machine was not enough for a
# real first start (the app was stopped for lack of memory while loading its
# embedding model); 4 GB leaves a margin. Not a measured minimum.
WINGS_MIN_MEM_GB=4

if [ -t 1 ]; then
  C_BOLD=$'\033[1m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RESET=$'\033[0m'
else
  C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_RESET=""
fi

info() { printf '%s==>%s %s\n' "$C_BOLD" "$C_RESET" "$*"; }
ok()   { printf '%s ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

# Read KEY from .env without sourcing it (values are never executed).
env_get() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  awk -v k="$key" -v q="'" '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      sub(/^export[[:space:]]+/, "", line)
      if (index(line, k "=") == 1) {
        v = substr(line, length(k) + 2)
        f = substr(v, 1, 1); l = substr(v, length(v), 1)
        if (length(v) >= 2 && f == l && (f == "\"" || f == q)) v = substr(v, 2, length(v) - 2)
        val = v
      }
    }
    END { if (val != "") print val }
  ' "$ENV_FILE"
}

# The file .env really is: a symlinked .env is updated where it points, so the
# link stays a link.
env_real_file() {
  local f="$ENV_FILE" link n=0
  while [ -L "$f" ] && [ "$n" -lt 20 ]; do
    link="$(readlink "$f")"
    case "$link" in /*) f="$link" ;; *) f="$(dirname "$f")/$link" ;; esac
    n=$((n + 1))
  done
  printf '%s' "$f"
}

# Replace .env with the finished temporary file $1 (made beside it): mode 600,
# the previous owner kept where the system allows, one atomic rename.
env_replace() {
  local tmp="$1" target
  target="$(env_real_file)"
  chmod 600 "$tmp"
  if [ -e "$target" ]; then
    chown --reference="$target" "$tmp" 2>/dev/null \
      || chown "$(stat -f '%u:%g' "$target" 2>/dev/null || echo x)" "$tmp" 2>/dev/null || true
  fi
  mv "$tmp" "$target"
}

# A temporary file beside the real .env, for env_replace.
env_tmp() { mktemp "$(env_real_file).XXXXXX"; }

# Set KEY=VALUE in .env: replaces an active line, else uncomments a
# "# KEY=" placeholder, else appends. VALUE must not contain newlines.
env_set() {
  local key="$1" value="$2" tmp
  case "$value" in *$'\n'*) die "refusing multi-line value for $key";; esac
  tmp="$(env_tmp)"
  awk -v k="$key" -v v="$value" '
    BEGIN { done = 0 }
    {
      line = $0
      if (!done && index(line, k "=") == 1) { print k "=" v; done = 1; next }
      if (!done && line ~ ("^#[[:space:]]*" k "=")) { print k "=" v; done = 1; next }
      print line
    }
    END { if (!done) print k "=" v }
  ' "$ENV_FILE" > "$tmp"
  env_replace "$tmp"
}

# Settings are named OPENWINGS_* in .env. Versions before 1.0.3-w2 used the
# upstream program's prefix instead (spelled in parts here so this file does
# not repeat it). migrate_env renames those keys in place; until it has run,
# setting() still reads a value saved under the earlier name.
OLD_PREFIX="ODY""SSEUS_"

# setting NAME: the value of OPENWINGS_NAME (or of the earlier name).
setting() {
  local v
  v="$(env_get "OPENWINGS_$1")"
  [ -n "$v" ] || v="$(env_get "${OLD_PREFIX}$1")"
  printf '%s' "$v"
}

# Rename every earlier-prefix key in .env to OPENWINGS_*, keeping each value
# byte for byte (also in commented lines, so a later env_set finds them, and
# in the comments OpenWings wrote). Other comment text is never changed.
# Safe to run any number of times: when nothing would change, nothing is written and nothing printed.
# Before changing anything it keeps a private copy (.env.bak, mode 600; an
# existing .env.bak is never overwritten, the copy then gets a timestamp).
# Stops without changing anything when a key is set under both names with two
# different non-empty values. Prints one line when it changed .env.
migrate_env() {
  local quiet="${1:-}" bak tmp conflict
  [ -f "$ENV_FILE" ] || return 0
  # Quick check: nothing to do unless the name appears at all.
  LC_ALL=C grep -qi "ody""sseus" "$ENV_FILE" || return 0
  # Active keys set under both names with different values: refuse to guess.
  # An empty value under one name is no conflict (the other one is kept).
  conflict="$(LC_ALL=C awk -v old="$OLD_PREFIX" '
    function keyof(line,   l) {
      l = line; sub(/^[[:space:]]*/, "", l); sub(/^export[[:space:]]+/, "", l)
      if (l !~ /^[A-Za-z_][A-Za-z0-9_]*=/) return ""
      return substr(l, 1, index(l, "=") - 1)
    }
    function valof(line,   l) { l = line; return substr(l, index(l, "=") + 1) }
    /^[[:space:]]*#/ { next }
    {
      k = keyof($0); if (k == "") next
      if (index(k, old) == 1) { o[substr(k, length(old) + 1)] = valof($0) }
      else if (index(k, "OPENWINGS_") == 1) { n[substr(k, 11)] = valof($0) }
    }
    END { for (s in o) if ((s in n) && n[s] != "" && o[s] != "" && n[s] != o[s]) print "OPENWINGS_" s }
  ' "$ENV_FILE")"
  if [ -n "$conflict" ]; then
    die "$(printf '%s' "$conflict" | head -n 1) is in .env twice, under its current and its earlier name, with different values. Keep the right value under the OPENWINGS_ name, delete the other line, then run this again. Nothing was changed."
  fi
  tmp="$(env_tmp)"
  LC_ALL=C awk -v old="$OLD_PREFIX" '
    function keyof(line,   l) {
      l = line; sub(/^[[:space:]]*/, "", l); sub(/^export[[:space:]]+/, "", l)
      if (l !~ /^[A-Za-z_][A-Za-z0-9_]*=/) return ""
      return substr(l, 1, index(l, "=") - 1)
    }
    function valof(line) { return substr(line, index(line, "=") + 1) }
    # Comments: only setting names with the earlier prefix are renamed (every
    # comment OpenWings ever shipped in .env.example names the program only
    # that way); other words people wrote are left as they are.
    function fixcomment(line) {
      gsub(old, "OPENWINGS_", line)
      return line
    }
    NR == FNR {
      if ($0 ~ /^[[:space:]]*#/) next
      k = keyof($0); if (k == "") next
      if (index(k, old) == 1) { s = substr(k, length(old) + 1); if (valof($0) != "") ov[s] = 1; os[s] = 1 }
      else if (index(k, "OPENWINGS_") == 1) { s = substr(k, 11); if (valof($0) != "") nv[s] = 1; ns[s] = 1 }
      next
    }
    {
      line = $0
      if (line ~ /^[[:space:]]*#/) {
        # A commented example of the base image setting carried the upstream
        # registry path as its value; the current example has none.
        if (line ~ ("^[[:space:]]*#[[:space:]]*" old "BASE_IMAGE=")) { print "# OPENWINGS_BASE_IMAGE="; next }
        print fixcomment(line); next
      }
      l = line; lead = ""
      if (match(l, /^[[:space:]]*(export[[:space:]]+)?/)) { lead = substr(l, 1, RLENGTH); l = substr(l, RLENGTH + 1) }
      k = keyof(l)
      if (k != "" && index(k, old) == 1) {
        s = substr(k, length(old) + 1); v = substr(l, length(k) + 2)
        # The same setting under the current name already: keep that one,
        # unless it is empty and this one is not.
        if ((s in nv) || ((s in ns) && v == "")) next
        k = "OPENWINGS_" s; l = k "=" v
      } else if (k != "" && index(k, "OPENWINGS_") == 1) {
        s = substr(k, 11)
        if (substr(l, length(k) + 2) == "" && (s in ov)) next   # empty; the earlier name has the value
      }
      if (k != "") {
        # References to other settings inside a value (${NAME} in Compose)
        # follow the rename; single-quoted values are literal and stay as they are.
        v = substr(l, length(k) + 2)
        if (substr(v, 1, 1) != "\047") { gsub("[$][{]" old, "${OPENWINGS_", v); gsub("[$]" old, "$OPENWINGS_", v) }
        l = k "=" v
      }
      print lead l
    }
  ' "$ENV_FILE" "$ENV_FILE" > "$tmp" || { rm -f "$tmp"; die "could not update .env; nothing was changed"; }
  if cmp -s "$tmp" "$ENV_FILE"; then rm -f "$tmp"; return 0; fi
  bak="${ENV_FILE}.bak"
  [ -e "$bak" ] && bak="${ENV_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
  ( umask 077; cp "$ENV_FILE" "$bak" ) || { rm -f "$tmp"; die "could not write a backup of .env; nothing was changed"; }
  chmod 600 "$bak"
  env_replace "$tmp"
  [ "$quiet" = "--quiet" ] || ok "Updated setting names in .env to OPENWINGS_* (values unchanged; previous copy: $(basename "$bak"))"
}

project_name() {
  local p
  p="$(env_get COMPOSE_PROJECT_NAME)"
  printf '%s' "${p:-wings}"
}

dc() { (cd "$WINGS_ROOT" && docker compose "$@"); }

require_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker Engine (Linux) or Docker Desktop (macOS/Windows) first."
  docker info >/dev/null 2>&1 || die "Docker is installed but the daemon is not reachable. Start Docker, or add your user to the docker group."
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 plugin is missing ('docker compose'). Install docker-compose-plugin."
  local v major minor
  v="$(docker compose version --short 2>/dev/null | sed 's/^v//')"
  major="${v%%.*}"; minor="${v#*.}"; minor="${minor%%.*}"
  case "$major" in ''|*[!0-9]*) return 0;; esac
  case "$minor" in ''|*[!0-9]*) minor=0;; esac
  if [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 24 ]; }; then
    die "Docker Compose $v is too old. OpenWings needs 2.24 or newer."
  fi
}

# Resolve a Compose-managed named volume (e.g. app-data) to its real name.
volume_name() {
  local short="$1" project
  project="$(project_name)"
  docker volume ls -q \
    --filter "label=com.docker.compose.project=${project}" \
    --filter "label=com.docker.compose.volume=${short}" | head -n 1
}

# Data volumes were renamed in 1.0.3-w2 (the earlier names carried the
# upstream program's name). Pairs of "earlier current" compose volume keys.
# The first three hold what cannot be made again (the database and settings,
# SSH keys, logs); the last two hold downloaded models and engines, which the
# app downloads again when they are missing.
VOLUME_RENAMES="ody""sseus-data:app-data ody""sseus-ssh:app-ssh ody""sseus-logs:app-logs ody""sseus-hf-cache:hf-cache ody""sseus-local:app-local"
VOLUME_OPTIONAL="hf-cache app-local"
# Label on a volume migrate_volumes created: the earlier volume it copies
# from, or (for the downloads above, when the disk was too full to copy them)
# the one it was left empty in place of. Labels are fixed when a volume is
# made, so they only say a copy was started; the files below say how it went.
MIGRATED_LABEL="org.openwings.migrated-from"
SKIPPED_LABEL="org.openwings.copy-skipped-from"
# In a new volume while its copy is unfinished (removed only once every copy
# of the run has been checked), and in a finished one: the earlier volume's
# name and its file count, bytes and newest change time when it was copied.
MV_COPYING=".openwings-copying"
MV_DONE=".openwings-migrated"
# Set in .env once the copy has finished, so a later run does not copy the
# earlier data again after the new volumes were deleted on purpose
# (`docker compose down -v`). ./scripts/migrate-volumes.sh --recopy does.
MV_RECORD="WINGS_VOLUMES_MIGRATED"

volume_label() { docker volume inspect --format "{{ index .Labels \"$2\" }}" "$1" 2>/dev/null || true; }

volume_is_optional() { case " $VOLUME_OPTIONAL " in *" $1 "*) return 0 ;; esac; return 1; }

# Whether a volume of that exact name exists (with or without labels).
volume_exists() { docker volume inspect "$1" >/dev/null 2>&1; }

# The earlier volume new volume $1 was created from by migrate_volumes, if any.
volume_copy_source() {
  local v
  v="$(volume_label "$1" "$MIGRATED_LABEL")"
  [ -n "$v" ] || v="$(volume_label "$1" "$SKIPPED_LABEL")"
  printf '%s' "$v"
}

# State of new volume $1 (helper image $2): "copying" (a copy into it never
# finished), "empty", or "done" followed by the lines of its record.
_mv_state() {
  docker run --rm --network none --entrypoint sh -v "${1}:/v:ro" "$2" -c "
    if [ -e /v/${MV_COPYING} ]; then echo copying
    elif [ -z \"\$(ls -A /v)\" ]; then echo empty
    else echo done; cat /v/${MV_DONE} 2>/dev/null || true; fi"
}

# Whether new volume $1 was made from earlier volume $2 by a finished copy
# ($3: helper image, found when not given).
volume_made_from() {
  local img="${3:-}" state
  [ -n "$2" ] && [ "$(volume_copy_source "$1")" = "$2" ] || return 1
  [ -n "$img" ] || img="$(_mv_image)" || return 1
  state="$(_mv_state "$1" "$img")" || return 1
  state="${state%%$'\n'*}"
  [ "$state" = "done" ] || { [ "$state" = "empty" ] && [ "$(volume_label "$1" "$SKIPPED_LABEL")" = "$2" ]; }
}

# File count, bytes and newest change time of the volume at $1 inside a
# helper container (the fingerprint kept in $MV_DONE), as shell code.
# shellcheck disable=SC2016  # expanded in the helper container
MV_STATS_SH='mv_stats() { (cd "$1" && printf "%s %s %s\n" "$(find . ! -name '"$MV_COPYING"' ! -name '"$MV_DONE"' | wc -l | tr -d " ")" "$(find . -type f ! -name '"$MV_COPYING"' ! -name '"$MV_DONE"' -printf "%s\n" | awk "{s+=\$1} END{print s+0}")" "$(find . ! -name '"$MV_COPYING"' ! -name '"$MV_DONE"' -printf "%T@\n" | sort -n | tail -n 1)"); }'

# Copies in progress, so an interrupted run can stop them and clean up.
_mv_created=""
_mv_stopped=""
_mv_copier() { printf 'openwings-volume-copy-%s' "$(project_name)"; }

# Undo an unfinished run (on any failure: die, Ctrl-C, a closed terminal):
# stop a running copy, remove every volume this run created (the earlier
# volumes are untouched, so nothing is lost), then start the app again on its
# earlier volumes. Nothing is left marked as copied.
_mv_abort() {
  local rc="${1:-$?}" v
  trap - EXIT INT TERM HUP
  [ -z "${1:-}" ] || warn "Interrupted. Undoing the unfinished copy; your data is unchanged in the previous volumes."
  docker rm -f "$(_mv_copier)" >/dev/null 2>&1 || true
  for v in $_mv_created; do
    docker volume rm "$v" >/dev/null 2>&1 || warn "could not remove the unfinished copy ${v}; it is not used. Remove it with: docker volume rm ${v}"
  done
  _mv_created=""
  if [ -n "$_mv_stopped" ]; then docker start "$_mv_stopped" >/dev/null 2>&1 || true; _mv_stopped=""; fi
  exit "$rc"
}
_mv_guard() {
  trap '_mv_abort' EXIT
  trap '_mv_abort 130' INT
  trap '_mv_abort 143' TERM
  trap '_mv_abort 129' HUP
}
_mv_unguard() { _mv_created=""; _mv_stopped=""; trap - EXIT INT TERM HUP; }

# Stop the app for a copy; on any later failure it is started again.
_mv_stop_app() {
  local project cid
  project="$(project_name)"
  _mv_guard
  cid="$(docker ps -q --filter "label=com.docker.compose.project=${project}" --filter "label=com.docker.compose.service=openwings" | head -n 1)"
  [ -n "$cid" ] || return 0
  docker stop "$cid" >/dev/null || die "could not stop OpenWings to copy its data; nothing was changed"
  _mv_stopped="$cid"
}

# An image with sh, cp, find and python for the copy helpers.
_mv_image() {
  local project cid img=""
  project="$(project_name)"
  cid="$(docker ps -aq --filter "label=com.docker.compose.project=${project}" --filter "label=com.docker.compose.service=openwings" | head -n 1)"
  [ -n "$cid" ] && img="$(docker inspect --format '{{.Config.Image}}' "$cid" 2>/dev/null || true)"
  if [ -z "$img" ] || ! docker image inspect "$img" >/dev/null 2>&1; then img="$(wings_image)" || return 1; fi
  [ -n "$img" ] || return 1
  printf '%s' "$img"
}

# Copy volume $1 into the new volume $2 with $3 as helper image, then compare
# file count and bytes and run SQLite's integrity check on every database in
# the copy (with its write-ahead log, read from a scratch copy). cp -a keeps
# owners, modes and times. $2 carries $MV_COPYING throughout. Prints the
# earlier volume's fingerprint.
_mv_copy() {
  docker run --rm --name "$(_mv_copier)" --network none --entrypoint sh -v "${1}:/from:ro" -v "${2}:/to" "$3" -c "
    set -e
    ${MV_STATS_SH}
    : > /to/${MV_COPYING}
    cp -a /from/. /to/
    a=\"\$(mv_stats /from)\"; b=\"\$(mv_stats /to)\"
    [ \"\${a% *}\" = \"\${b% *}\" ] || { echo \"copy differs: \${a% *} vs \${b% *}\" >&2; exit 1; }
    find /to -type f \\( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \\) | while read -r db; do
      python - \"\$db\" <<'PY' || { echo \"database check failed: \${db#/to/}\" >&2; exit 1; }
import os, shutil, sqlite3, sys, tempfile
src = sys.argv[1]
d = tempfile.mkdtemp()
try:
    t = os.path.join(d, 'c.db')
    shutil.copyfile(src, t)
    if os.path.exists(src + '-wal') and os.path.getsize(src + '-wal') > 0:
        shutil.copyfile(src + '-wal', t + '-wal')
    c = sqlite3.connect(t)
    r = c.execute('PRAGMA integrity_check').fetchone()[0]
    c.close()
finally:
    shutil.rmtree(d, ignore_errors=True)
sys.exit(0 if r == 'ok' else 1)
PY
    done
    echo \"\$a\"
  "
}

# Mark new volume $1 finished: earlier volume $2, fingerprint $3.
_mv_finish() {
  docker run --rm --network none --entrypoint sh -v "${1}:/to" "$4" -c "
    printf '%s\n%s\n' '$2' '$3' > /to/${MV_DONE} && rm -f /to/${MV_COPYING}"
}

# A volume with the new name that Compose did not make (no labels, e.g. from
# a plain `docker run -v`) is never copied into or used.
_mv_refuse_foreign() {
  volume_exists "$1" || return 0
  die "A volume named $1 already exists but was not made by OpenWings (it has no Compose labels), so it was not used. Check what it holds (docker run --rm -v $1:/v alpine ls -la /v), keep anything you need elsewhere, remove it with: docker volume rm $1, then run this again. Nothing was changed."
}

_mv_create() {  # name, compose key, label, label value
  _mv_refuse_foreign "$1"
  docker volume create \
    --label "com.docker.compose.project=$(project_name)" \
    --label "com.docker.compose.volume=$2" \
    --label "com.docker.compose.version=$(docker compose version --short 2>/dev/null | sed 's/^v//')" \
    --label "$3=$4" \
    "$1" >/dev/null || return 1
  _mv_created="$_mv_created $1"
}

_mv_kb() { docker run --rm --network none --entrypoint sh -v "${1}:/from:ro" "$2" -c 'du -sk /from | cut -f1'; }

# Earlier data volumes of this project that still exist (by name).
earlier_volumes() {
  local pair v out=""
  for pair in $VOLUME_RENAMES; do
    v="$(volume_name "${pair%%:*}")"; [ -n "$v" ] && out="$out $v"
  done
  printf '%s' "${out# }"
}

# Copy each earlier data volume into its new name, once. The earlier volumes
# are only read, never changed or removed, so they stay as the way back until
# `./scripts/migrate-volumes.sh --remove-old`. The app is stopped while its
# data is copied. Every copy is checked; only when all of them are good are
# they marked finished. Any failure or interruption before that removes every
# volume this run created and starts the app again on the earlier volumes,
# so a later run starts the copy afresh.
#
# A new volume that exists next to an earlier one but was not made by this
# copy was created empty (the app was started before its data was moved);
# for the database, SSH keys and logs that stops here instead of running on
# without the data. `./scripts/migrate-volumes.sh --recopy` sets it aside and
# copies again (it also copies again when the new volumes were deleted after
# an earlier copy; set _MV_FORCE=1 for that).
migrate_volumes() {
  local pair old new oldv newv project todo="" img="" need=0 free kb skip="" gone="" src state stats v
  project="$(project_name)"
  docker rm -f "$(_mv_copier)" >/dev/null 2>&1 || true
  for pair in $VOLUME_RENAMES; do
    old="${pair%%:*}"; new="${pair##*:}"
    oldv="$(volume_name "$old")"; newv="$(volume_name "$new")"
    [ -n "$oldv" ] || continue
    if [ -z "$newv" ]; then
      if [ -n "$(env_get "$MV_RECORD")" ] && [ -z "${_MV_FORCE:-}" ]; then gone="$gone $oldv"; continue; fi
      _mv_refuse_foreign "${project}_${new}"
      todo="$todo $pair"; continue
    fi
    [ -n "$(env_get "$MV_RECORD")" ] && continue
    src="$(volume_copy_source "$newv")"
    if [ "$src" = "$oldv" ]; then
      [ -n "$img" ] || img="$(_mv_image)" || die "could not find or build the OpenWings image for the copy; nothing was changed"
      state="$(_mv_state "$newv" "$img")" || die "could not read ${newv}; nothing was changed"
      state="${state%%$'\n'*}"
      case "$state" in
        done) continue ;;
        empty) [ "$(volume_label "$newv" "$SKIPPED_LABEL")" = "$oldv" ] && continue ;;
      esac
      # A copy that was interrupted (the computer or Docker stopped mid-copy).
      if [ -n "$(docker ps -aq --filter "volume=${newv}")" ]; then
        die "${newv} holds an unfinished copy of ${oldv}, and a container uses it. Your data is safe in ${oldv}. Run ./scripts/migrate-volumes.sh --recopy to set ${newv} aside and copy again, then run this again."
      fi
      docker volume rm "$newv" >/dev/null || die "could not remove the unfinished copy ${newv}; your data is safe in ${oldv}"
      warn "removed ${newv}, an unfinished copy of ${oldv} from an interrupted run; copying again"
      todo="$todo $pair"; continue
    fi
    if volume_is_optional "$new"; then
      warn "${newv} was created before the downloads in ${oldv} were copied; keeping it (the app downloads what is missing). ${oldv} is kept."
    else
      die "${newv} was created empty before your data was copied from ${oldv}, so OpenWings would start without your data. Your data is safe in ${oldv}. Run ./scripts/migrate-volumes.sh --recopy to set ${newv} aside and copy your data again, then run this again."
    fi
  done
  if [ -n "$gone" ]; then
    warn "Your data was copied to the new volume names earlier, and those copies have since been removed (for example with docker compose down -v). OpenWings starts with empty data; the copy is not made again."
    warn "  The earlier volumes still hold the data from before that copy:${gone}"
    warn "  To get that data back: ./scripts/migrate-volumes.sh --recopy. To delete it: docker volume rm${gone}"
  fi
  if [ -z "$todo" ]; then
    if [ -z "$(env_get "$MV_RECORD")" ] && [ -n "$(earlier_volumes)" ]; then env_set "$MV_RECORD" 1; fi
    return 0
  fi
  info "Moving your data to the new volume names (a one-time copy; the previous volumes are kept)"
  [ -n "$img" ] || img="$(_mv_image)" || die "could not find or build the OpenWings image for the copy; nothing was changed"
  # A copy run (only with --recopy once recorded) is not finished until it is.
  [ -z "$(env_get "$MV_RECORD")" ] || env_set "$MV_RECORD" ""
  _mv_stop_app
  # Room for the copies: bytes of the earlier volumes vs free space where
  # Docker keeps volumes. The database, keys and logs come first; downloads
  # that do not fit are left to be downloaded again.
  oldv="$(volume_name "$(printf '%s' "${todo# }" | cut -d' ' -f1 | cut -d: -f1)")"
  free="$(docker run --rm --network none --entrypoint sh -v "${oldv}:/from:ro" "$img" -c 'df -Pk /from | awk "NR==2{print \$4}"')"
  case "$free" in ''|*[!0-9]*) free=0 ;; esac
  for pair in $todo; do
    volume_is_optional "${pair##*:}" && continue
    kb="$(_mv_kb "$(volume_name "${pair%%:*}")" "$img")"; need=$(( need + ${kb:-0} ))
  done
  if [ "$free" -gt 0 ] && [ "$need" -gt $(( free - 1048576 )) ]; then
    die "Not enough disk space to copy your data to the new volume names (needs about $(( need / 1024 )) MB plus 1 GB spare, $(( free / 1024 )) MB free). Free some space, then run this again. Nothing was changed."
  fi
  for pair in $todo; do
    volume_is_optional "${pair##*:}" || continue
    kb="$(_mv_kb "$(volume_name "${pair%%:*}")" "$img")"
    if [ "$free" -gt 0 ] && [ $(( need + ${kb:-0} )) -gt $(( free - 1048576 )) ]; then
      skip="$skip ${pair##*:}"
    else
      need=$(( need + ${kb:-0} ))
    fi
  done
  # Copy and check every volume first ...
  local done_list=""
  for pair in $todo; do
    old="${pair%%:*}"; new="${pair##*:}"
    oldv="$(volume_name "$old")"; newv="${project}_${new}"
    case " $skip " in
      *" $new "*)
        _mv_create "$newv" "$new" "$SKIPPED_LABEL" "$oldv" || die "could not create the volume ${newv}; nothing was switched"
        warn "Not enough disk space to copy ${new} (downloaded models or engines); it starts empty and the app downloads them again. The previous copy stays in ${oldv} until --remove-old."
        continue ;;
    esac
    _mv_create "$newv" "$new" "$MIGRATED_LABEL" "$oldv" || die "could not create the volume ${newv}; nothing was switched"
    stats="$(_mv_copy "$oldv" "$newv" "$img")" || stats=""
    stats="$(printf '%s\n' "$stats" | tail -n 1)"
    [ -n "$stats" ] || die "Copying ${new} failed (see above). Your data is unchanged in the previous volumes; nothing was switched."
    done_list="$done_list ${newv}=${oldv}=${stats// /_}"
    ok "copied ${new}"
  done
  # ... and only then mark them finished.
  for v in $done_list; do
    newv="${v%%=*}"; v="${v#*=}"; oldv="${v%%=*}"; stats="${v#*=}"
    _mv_finish "$newv" "$oldv" "${stats//_/ }" "$img" || die "could not mark ${newv} as copied; nothing was switched"
  done
  # Copied: the caller starts the app on the new volumes.
  _mv_unguard
  env_set "$MV_RECORD" 1
  ok "Your data now lives in the new volumes. The previous ones are kept unchanged as a fallback;"
  ok "  once everything works, remove them with: ./scripts/migrate-volumes.sh --remove-old"
}

# Set aside new volumes that were not made by a finished copy (created empty
# before the data was copied, or an interrupted copy), renamed to
# <name>-unmigrated-<time> with nothing deleted, then copy the earlier data
# again, also where the new volumes were removed after an earlier copy.
recopy_volumes() {
  local pair new oldv newv aside img="" ts n=0
  ts="$(date +%Y%m%d-%H%M%S)"
  for pair in $VOLUME_RENAMES; do
    new="${pair##*:}"
    oldv="$(volume_name "${pair%%:*}")"; newv="$(volume_name "$new")"
    if [ -z "$oldv" ] || [ -z "$newv" ]; then continue; fi
    [ -n "$img" ] || img="$(_mv_image)" || die "could not find or build the OpenWings image; nothing was changed"
    volume_made_from "$newv" "$oldv" "$img" && continue
    [ -n "$_mv_stopped" ] || _mv_stop_app
    if [ -n "$(docker ps -aq --filter "volume=${newv}")" ]; then
      docker ps -aq --filter "volume=${newv}" | xargs docker rm -f >/dev/null 2>&1 || true
    fi
    aside="${newv}-unmigrated-${ts}"
    docker volume create --label "org.openwings.set-aside-from=${newv}" "$aside" >/dev/null || die "could not create ${aside}; nothing was changed"
    docker run --rm --network none --entrypoint sh -v "${newv}:/from:ro" -v "${aside}:/to" "$img" -c "set -e; ${MV_STATS_SH}; cp -a /from/. /to/; [ \"\$(mv_stats /from | cut -d' ' -f1-2)\" = \"\$(mv_stats /to | cut -d' ' -f1-2)\" ]" \
      || { docker volume rm "$aside" >/dev/null 2>&1 || true; die "could not set ${newv} aside; nothing was changed"; }
    docker volume rm "$newv" >/dev/null || die "could not remove ${newv} after setting it aside as ${aside}"
    ok "set ${newv} aside as ${aside} (kept; remove it with docker volume rm once you do not need it)"
    n=$((n + 1))
  done
  [ "$n" -gt 0 ] || ok "No volume needs to be set aside."
  _MV_FORCE=1 migrate_volumes
}

container_id() { dc ps -q "$1" 2>/dev/null | head -n 1; }

# Wait until a service reports healthy. $1 service, $2 timeout seconds.
# Fails fast when the container exits or restarts (a crash loop, e.g. the
# OpenWings preflight refusing a mismatched key with exit code 78).
wait_healthy() {
  local svc="$1" timeout="${2:-600}" waited=0 cid status restarts start_restarts=""
  while [ "$waited" -lt "$timeout" ]; do
    cid="$(container_id "$svc")"
    if [ -n "$cid" ]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)"
      restarts="$(docker inspect --format '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)"
      [ -n "$start_restarts" ] || start_restarts="$restarts"
      case "$status" in
        healthy) return 0 ;;
        exited|dead) return 1 ;;
      esac
      if [ "$restarts" != "$start_restarts" ] || \
         [ "$(docker inspect --format '{{.State.Restarting}}' "$cid" 2>/dev/null)" = "true" ]; then
        return 1
      fi
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

# Memory Docker can use, in whole GB (0 when Docker cannot say).
docker_mem_gb() {
  local bytes
  bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || true)"
  case "$bytes" in ''|*[!0-9]*) echo 0 ;; *) echo $(( (bytes + 536870912) / 1073741824 )) ;; esac
}

# Say in plain words why a service did not come up, when Docker can tell.
# Docker resets a container's exit code and out of memory flag when it
# restarts, so this reads the container's recent events instead.
explain_unhealthy() {
  local svc="$1" cid events
  cid="$(container_id "$svc")"
  if [ -z "$cid" ]; then warn "The $svc container was not created; see the messages above."; return 0; fi
  events="$(docker events --since 30m --until "$(( $(date +%s) + 2 ))" --filter "container=$cid" \
    --filter event=die --filter event=oom --format '{{.Action}} {{index .Actor.Attributes "exitCode"}}' 2>/dev/null || true)"
  case "$events" in
    *oom*|*"die 137"*)
      warn "OpenWings ran out of memory and was stopped (Docker has about $(docker_mem_gb) GB)."
      warn "Give Docker at least ${WINGS_MIN_MEM_GB} GB, then run this again:"
      warn "  Colima:         colima stop && colima start --memory ${WINGS_MIN_MEM_GB}"
      warn "  Docker Desktop: Settings > Resources > Memory"
      warn "  OrbStack:       Settings > System > Memory limit" ;;
    *"die 78"*)
      warn "OpenWings refused its encryption key (exit 78): the data was made with another key. See README, Troubleshooting, or run ./scripts/adopt-key.sh." ;;
    *die*)
      warn "OpenWings stopped unexpectedly ($(printf '%s\n' "$events" | grep '^die' | tail -n 1)). The last lines of its log:"
      dc logs --tail 30 "$svc" 2>/dev/null || true ;;
    *)
      warn "OpenWings was still starting when setup gave up. The last lines of its log:"
      dc logs --tail 30 "$svc" 2>/dev/null || true ;;
  esac
}

# Run a command with its output appended to $LOG, printing a dot every 10
# seconds so a long step visibly moves. Returns the command's status.
with_dots() {
  local pid rc=0 n=0
  "$@" >> "$LOG" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1; n=$((n + 1))
    if [ $((n % 10)) -eq 0 ]; then printf '.'; fi
  done
  wait "$pid" || rc=$?
  if [ "$n" -ge 10 ]; then printf '\n'; fi
  return "$rc"
}

# Random secrets with no shell-special characters.
rand_token() { head -c "${1:-24}" /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'; }
# A Fernet key: 32 random bytes, URL-safe base64, padded (44 chars).
fernet_key() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; }

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# First 12 hex chars of the SHA-256 of stdin. Used to fingerprint the
# encryption key in backup manifests without revealing it.
sha256_stdin_short() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -c1-12
  else shasum -a 256 | cut -c1-12; fi
}

# The locally built OpenWings image (built on demand), for one-off helper
# containers that need python/tar but must not start the app.
wings_image() {
  local img
  img="$(cd "$WINGS_ROOT" && docker compose config --images 2>/dev/null | grep -m1 '^openwings:' || true)"
  [ -n "$img" ] || die "could not resolve the OpenWings image name from docker-compose.yml"
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    info "Building ${img}" >&2
    dc build openwings 2>&1 | public_output >&2
    [ "${PIPESTATUS[0]}" -eq 0 ] || die "building ${img} failed (see above)"
  fi
  printf '%s' "$img"
}

# Image tags from before the app service was renamed, still on hosts that
# installed early. Removes only the tag, and only when no container uses it.
remove_old_image_tags() {
  local ref
  for ref in $(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -E '^wings/ody''sseus:' || true); do
    if [ -z "$(docker ps -aq --filter "ancestor=${ref}" 2>/dev/null)" ]; then
      docker image rm "$ref" >/dev/null 2>&1 || true
    fi
  done
}

# Docker's build output names the base image by its registry path; show it as
# "the pinned base image" instead (the reference itself is in the Dockerfile
# and NOTICE.md). Also used for the setting names in Compose's own messages.
public_output() {
  LC_ALL=C sed -E 's#wings/ody''sseus:#openwings:#g; s#[A-Za-z0-9._/-]*[Oo]dy''sseus[A-Za-z0-9._/:@-]*#<pinned base image>#g; s#ODY''SSEUS_#OPENWINGS_#g; s#ODY''SSEUS#OPENWINGS#g'
}

# Reword an existing install.log once (earlier runs logged the upstream names
# that build output used to show). Kept private like the log itself.
scrub_log() {
  local log="$1" t
  [ -f "$log" ] && LC_ALL=C grep -qi 'ody''sseus' "$log" || return 0
  t="$(mktemp "${log}.XXXXXX")" || return 0
  if public_output < "$log" > "$t"; then chmod 600 "$t"; mv "$t" "$log"; else rm -f "$t"; fi
}

# Get every image the stack needs before anything is stopped: pull the ones
# not on this computer yet and build the app (setup.sh, upgrade.sh). The data
# copy then happens between stopping the old app and starting the new one, and
# a failed download or build leaves the running app alone.
prepare_images() {
  { dc pull --ignore-buildable --policy missing --quiet && dc build; } 2>&1 | public_output
  return "${PIPESTATUS[0]}"
}

# Start the stack from the images prepare_images made (setup.sh).
start_stack() {
  dc up -d --remove-orphans 2>&1 | public_output
  return "${PIPESTATUS[0]}"
}

# Data volumes (earlier and current names) of this project that exist, by name.
data_volumes_present() {
  local pair key v out="" project
  project="$(project_name)"
  for pair in $VOLUME_RENAMES; do
    for key in "${pair%%:*}" "${pair##*:}"; do
      v="$(volume_name "$key")"
      [ -n "$v" ] || { volume_exists "${project}_${key}" && v="${project}_${key}"; }
      [ -n "$v" ] && out="$out $v"
    done
  done
  printf '%s' "${out# }"
}
