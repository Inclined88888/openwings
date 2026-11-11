# shellcheck shell=bash
# Shared helpers for the OpenWings scripts. Sourced, not executed.
# Portable to the bash 3.2 that ships with macOS: no associative arrays,
# no ${var,,}, no mapfile.

WINGS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${WINGS_ROOT}/.env"

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

# Set KEY=VALUE in .env: replaces an active line, else uncomments a
# "# KEY=" placeholder, else appends. VALUE must not contain newlines.
env_set() {
  local key="$1" value="$2" tmp
  case "$value" in *$'\n'*) die "refusing multi-line value for $key";; esac
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
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
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
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

# Resolve a Compose-managed named volume (e.g. odysseus-data) to its real name.
volume_name() {
  local short="$1" project
  project="$(project_name)"
  docker volume ls -q \
    --filter "label=com.docker.compose.project=${project}" \
    --filter "label=com.docker.compose.volume=${short}" | head -n 1
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
  img="$(cd "$WINGS_ROOT" && docker compose config --images 2>/dev/null | grep -m1 '^wings/odysseus' || true)"
  [ -n "$img" ] || die "could not resolve the OpenWings image name from docker-compose.yml"
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    info "Building ${img}" >&2
    dc build odysseus >&2
  fi
  printf '%s' "$img"
}
