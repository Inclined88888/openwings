#!/usr/bin/env bash
# Wings first-run setup.
#
#   ./scripts/setup.sh                      local install on http://localhost:7011
#   ./scripts/setup.sh --with-ollama        also run a bundled Ollama for local models
#   ./scripts/setup.sh --domain ai.example.com --acme-email you@example.com
#                                           public HTTPS via Caddy + Let's Encrypt
#   ./scripts/setup.sh --with-caldav        also run a private CalDAV calendar server
#   ./scripts/setup.sh --with-browser       also run a real browser the agent can use
#
# Safe to re-run: existing secrets in .env are never regenerated or overwritten.
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --domain NAME          Serve https://NAME through the bundled Caddy (profile https)
  --acme-email EMAIL     Contact address for Let's Encrypt (required with --domain)
  --with-ollama          Run the bundled Ollama container (profile ollama)
  --ollama-gpu nvidia    Give the bundled Ollama the NVIDIA GPU (needs nvidia-container-toolkit)
  --ollama-model NAME    Pull this model into the bundled Ollama after start (e.g. qwen3:4b)
  --with-caldav          Run the bundled Radicale CalDAV server (profile caldav)
  --caldav-user NAME     CalDAV username to create (default: wings)
  --with-browser         Run the agent browser with live view at /wings/browser (profile browser)
  --bind ADDR            Host address for the web UI (default 127.0.0.1; 0.0.0.0 = whole LAN)
  --port N               Host port for the web UI (default 7011)
  --no-start             Write .env and config only; do not build or start anything
  --no-agents            Do not add the starter agents on first install
  -h, --help             Show this help
EOF
}

DOMAIN=""; ACME_EMAIL=""; WITH_OLLAMA=0; OLLAMA_GPU=""; OLLAMA_MODEL=""
WITH_CALDAV=0; CALDAV_USER="wings"; BIND=""; PORT=""; START=1; SEED_AGENTS=1; WITH_BROWSER=0

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
    --acme-email) ACME_EMAIL="${2:?--acme-email needs a value}"; shift 2 ;;
    --with-ollama) WITH_OLLAMA=1; shift ;;
    --ollama-gpu) OLLAMA_GPU="${2:?--ollama-gpu needs a value}"; WITH_OLLAMA=1; shift 2 ;;
    --ollama-model) OLLAMA_MODEL="${2:?--ollama-model needs a value}"; WITH_OLLAMA=1; shift 2 ;;
    --with-caldav) WITH_CALDAV=1; shift ;;
    --caldav-user) CALDAV_USER="${2:?--caldav-user needs a value}"; WITH_CALDAV=1; shift 2 ;;
    --with-browser) WITH_BROWSER=1; shift ;;
    --bind) BIND="${2:?--bind needs a value}"; shift 2 ;;
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --no-start) START=0; shift ;;
    --no-agents) SEED_AGENTS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

case "$DOMAIN" in
  ''|*.*) ;;
  *) die "--domain must be a fully qualified name such as ai.example.com" ;;
esac
case "$DOMAIN" in *[!A-Za-z0-9.-]*) die "--domain contains invalid characters";; esac
if [ -n "$DOMAIN" ] && [ -z "$ACME_EMAIL" ] && [ -z "$(env_get WINGS_ACME_EMAIL 2>/dev/null)" ]; then
  die "--domain needs --acme-email you@example.com (Let's Encrypt contact; Caddy will not start without it)"
fi
case "$ACME_EMAIL" in ''|*@*.*) ;; *) die "--acme-email does not look like an email address";; esac
case "$CALDAV_USER" in ''|*[!A-Za-z0-9._-]*) die "--caldav-user may contain only letters, digits, dot, dash and underscore";; esac
case "$OLLAMA_GPU" in ''|nvidia) ;; *) die "--ollama-gpu supports only: nvidia";; esac
case "$PORT" in ''|*[!0-9]*) [ -z "$PORT" ] || die "--port must be a number";; esac

[ "$START" -eq 1 ] && require_docker

# 1. .env ---------------------------------------------------------------
umask 077
if [ ! -f "$ENV_FILE" ]; then
  cp "$WINGS_ROOT/.env.example" "$ENV_FILE"
  info "Created .env from .env.example"
fi
chmod 600 "$ENV_FILE"

NEW_ADMIN_PASSWORD=0
ensure_secret() {
  local key="$1" generator="$2"
  if [ -z "$(env_get "$key")" ]; then
    env_set "$key" "$($generator)"
    ok "generated $key"
    [ "$key" = "ODYSSEUS_ADMIN_PASSWORD" ] && NEW_ADMIN_PASSWORD=1
  fi
  return 0
}
gen_password() { rand_token 18; }
gen_searxng() { rand_token 36; }

ensure_secret ODYSSEUS_APP_KEY fernet_key
ensure_secret ODYSSEUS_ADMIN_PASSWORD gen_password
ensure_secret SEARXNG_SECRET gen_searxng
# Shared secret between the app and the browser container (profile browser).
ensure_secret WINGS_BROWSER_TOKEN gen_searxng

# Upstream silently skips creating the admin for passwords under 8 characters
# and leaves the web setup page open to whoever reaches it first.
admin_pw="$(env_get ODYSSEUS_ADMIN_PASSWORD)"
[ "${#admin_pw}" -ge 8 ] || die "ODYSSEUS_ADMIN_PASSWORD in .env must be at least 8 characters"
case "$admin_pw" in *[[:space:]]*) die "ODYSSEUS_ADMIN_PASSWORD must not contain spaces";; esac

[ -n "$BIND" ] && env_set APP_BIND "$BIND"
[ -n "$PORT" ] && env_set APP_PORT "$PORT"
APP_PORT_V="$(env_get APP_PORT)"; APP_PORT_V="${APP_PORT_V:-7011}"

# The browser origin must be allowed for login and CSRF checks.
if [ -z "$DOMAIN" ] && [ -n "$PORT" ]; then
  env_set ALLOWED_ORIGINS "http://localhost:${APP_PORT_V},http://127.0.0.1:${APP_PORT_V}"
fi

# 2. Profiles -----------------------------------------------------------
profiles="$(env_get COMPOSE_PROFILES)"
add_profile() {
  case ",${profiles}," in *",$1,"*) ;; *) profiles="${profiles:+${profiles},}$1" ;; esac
}

if [ -n "$DOMAIN" ]; then
  add_profile https
  proxy_ip="$(env_get WINGS_PROXY_IP)"; proxy_ip="${proxy_ip:-172.30.57.250}"
  env_set WINGS_DOMAIN "$DOMAIN"
  [ -n "$ACME_EMAIL" ] && env_set WINGS_ACME_EMAIL "$ACME_EMAIL"
  env_set ALLOWED_ORIGINS "https://${DOMAIN}"
  env_set SECURE_COOKIES true
  env_set OAUTH_REDIRECT_BASE_URL "https://${DOMAIN}"
  env_set FORWARDED_ALLOW_IPS "$proxy_ip"
  [ -z "$(env_get GOOGLE_OAUTH_REDIRECT_URI)" ] && \
    env_set GOOGLE_OAUTH_REDIRECT_URI "https://${DOMAIN}/api/email/oauth/google/callback"
  ok "HTTPS enabled for https://${DOMAIN}"
fi

if [ "$WITH_OLLAMA" -eq 1 ]; then
  add_profile ollama
  [ -z "$(env_get OLLAMA_BASE_URL)" ] && env_set OLLAMA_BASE_URL "http://ollama:11434/v1"
  if [ "$OLLAMA_GPU" = "nvidia" ]; then
    env_set COMPOSE_FILE "docker-compose.yml:compose/ollama-nvidia.yml"
  fi
  ok "bundled Ollama enabled"
fi

if [ "$WITH_CALDAV" -eq 1 ]; then
  add_profile caldav
  env_set ODYSSEUS_ALLOW_PRIVATE_CALDAV 1
  users_file="$WINGS_ROOT/config/radicale/users"
  if [ -d "$users_file" ]; then
    die "$users_file is a directory (Docker creates one when the file is missing). Remove it (sudo rmdir \"$users_file\") and re-run."
  fi
  if [ ! -s "$users_file" ] || ! grep -q "^${CALDAV_USER}:" "$users_file"; then
    [ "$START" -eq 1 ] || require_docker
    pw="$(env_get RADICALE_PASSWORD)"
    if [ -z "$pw" ]; then pw="$(rand_token 18)"; env_set RADICALE_PASSWORD "$pw"; fi
    env_set RADICALE_USER "$CALDAV_USER"
    radicale_image="$(cd "$WINGS_ROOT" && docker compose --profile caldav config --images 2>/dev/null | grep -m1 radicale || true)"
    [ -n "$radicale_image" ] || die "could not resolve the radicale image from docker-compose.yml"
    # Hash with the bcrypt that ships inside the Radicale image; the password
    # travels over stdin, never on a command line.
    hash="$(printf '%s' "$pw" | docker run --rm -i --network none --entrypoint /venv/bin/python "$radicale_image" \
      -c 'import sys, bcrypt; print(bcrypt.hashpw(sys.stdin.buffer.read(), bcrypt.gensalt(rounds=12)).decode())')"
    # shellcheck disable=SC2016  # literal bcrypt prefix
    case "$hash" in '$2'*) ;; *) die "bcrypt hashing failed";; esac
    touch "$users_file"
    grep -v "^${CALDAV_USER}:" "$users_file" > "${users_file}.tmp" || true
    printf '%s:%s\n' "$CALDAV_USER" "$hash" >> "${users_file}.tmp"
    # Rewrite in place (same inode) so a running Radicale sees the change
    # through its bind mount.
    cat "${users_file}.tmp" > "$users_file"
    rm -f "${users_file}.tmp"
    # The Radicale container runs as uid 2999 and must be able to read this.
    chmod 644 "$users_file"
    ok "CalDAV user '${CALDAV_USER}' created"
  fi
fi

if [ "$WITH_BROWSER" -eq 1 ]; then
  add_profile browser
  ok "agent browser enabled (live view at /wings/browser once started)"
fi

[ -n "$profiles" ] && env_set COMPOSE_PROFILES "$profiles"

if [ "$START" -eq 0 ]; then
  ok ".env is ready. Start with: docker compose up -d --build"
  exit 0
fi

# 3. Build and start -----------------------------------------------------
info "Building the Wings image and starting the stack (the first run downloads several GB of images)"
dc up -d --build

info "Waiting for Odysseus to become healthy (first boot also downloads the embedding model)"
if ! wait_healthy odysseus 900; then
  dc ps
  dc logs --tail 80 odysseus || true
  die "Odysseus did not become healthy. See the logs above and the README section Troubleshooting."
fi
ok "Odysseus is healthy"

if [ -n "$OLLAMA_MODEL" ]; then
  if wait_healthy ollama 300; then
    info "Pulling ${OLLAMA_MODEL} into the bundled Ollama (this can take a while)"
    dc exec -T ollama ollama pull "$OLLAMA_MODEL" || warn "model pull failed; retry with: docker compose exec ollama ollama pull ${OLLAMA_MODEL}"
  else
    warn "Ollama is not healthy yet; pull later with: docker compose exec ollama ollama pull ${OLLAMA_MODEL}"
  fi
fi

# Starter agents, first install only (re-run ./scripts/seed-agents.sh any time).
if [ "$NEW_ADMIN_PASSWORD" -eq 1 ] && [ "$SEED_AGENTS" -eq 1 ]; then
  info "Adding starter agents"
  "$WINGS_ROOT/scripts/seed-agents.sh" || warn "could not add starter agents; run ./scripts/seed-agents.sh later"
fi

# 4. Summary -------------------------------------------------------------
if [ -n "$DOMAIN" ]; then url="https://${DOMAIN}"; else
  bind_v="$(env_get APP_BIND)"; bind_v="${bind_v:-127.0.0.1}"
  [ "$bind_v" = "0.0.0.0" ] && bind_v="localhost"
  url="http://${bind_v}:${APP_PORT_V}"
fi
admin_user="$(env_get ODYSSEUS_ADMIN_USER)"; admin_user="${admin_user:-admin}"
if [ "$NEW_ADMIN_PASSWORD" -eq 1 ]; then
  shown_password="$(env_get ODYSSEUS_ADMIN_PASSWORD)   (shown once; also in .env)"
else
  shown_password="unchanged (grep ^ODYSSEUS_ADMIN_PASSWORD= .env)"
fi

cat <<EOF

${C_BOLD}Wings is running.${C_RESET}

  Open:      ${url}
  Username:  ${admin_user}
  Password:  ${shown_password}

Next:
  1. Sign in, then open Settings and add a model provider (see README, "Models").
  2. Turn on 2FA in Settings, then change the admin password there.
  3. Copy .env somewhere safe and offline. It holds the encryption key;
     backups cannot be decrypted without it.
EOF

if [ "$WITH_CALDAV" -eq 1 ]; then
  cat <<EOF

  CalDAV:    user $(env_get RADICALE_USER), password in .env as RADICALE_PASSWORD
             Odysseus URL: http://radicale:5232/$(env_get RADICALE_USER)/
EOF
fi
