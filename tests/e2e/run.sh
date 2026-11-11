#!/usr/bin/env bash
# Run the Wings end-to-end checks against the stack in this checkout.
#
#   ./tests/e2e/run.sh
#
# Starts two throwaway helper containers on the stack's network: a
# deterministic mock model (tests/e2e/mock_llm.py) and a GreenMail SMTP/IMAP
# test server with two local mailboxes. Nothing is sent to the internet and no
# real mailbox is touched. The checks create a test chat, agent, email account
# and MCP server in your instance; run it on a test install, not your daily one.
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib.sh"

require_docker
project="$(project_name)"
net="${project}_wings"
docker network inspect "$net" >/dev/null 2>&1 || die "network $net not found; is the stack running?"

created=""
# shellcheck disable=SC2086  # $created is a space-separated list
cleanup() { [ -n "$created" ] && docker rm -f $created >/dev/null 2>&1 || true; }
[ "${KEEP_HELPERS:-0}" = "1" ] || trap cleanup EXIT

# Reuse running helpers: recreating them gives the app's pooled keep-alive
# connections a dead peer for one request and trips its 20 s host cooldown.
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
if ! running "${project}-mockllm"; then
  docker rm -f "${project}-mockllm" >/dev/null 2>&1 || true
  docker run -d --name "${project}-mockllm" --network "$net" --network-alias mockllm \
    -v "${WINGS_ROOT}/tests/e2e:/t:ro" docker.io/library/python:3.14.9-slim python /t/mock_llm.py >/dev/null
  created="$created ${project}-mockllm"
fi
if ! running "${project}-greenmail"; then
  docker rm -f "${project}-greenmail" >/dev/null 2>&1 || true
  docker run -d --name "${project}-greenmail" --network "$net" --network-alias mail \
    -e GREENMAIL_OPTS="-Dgreenmail.setup.test.smtp -Dgreenmail.setup.test.imap -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=me:mailpass123@example.test,friend:friendpass@example.test" \
    docker.io/greenmail/standalone:2.1.14@sha256:1ef95a966418cd09b7ea91d504d8c0826bbe7a2f6e679a75c601a831587c1626 >/dev/null
  created="$created ${project}-greenmail"
fi
case ",$(env_get COMPOSE_PROFILES)," in
  *,browser,*)
    # Static test pages for the browser checks, on the browser's own network.
    if ! running "${project}-testsite"; then
      docker rm -f "${project}-testsite" >/dev/null 2>&1 || true
      docker run -d --name "${project}-testsite" --network "${project}_browser" --network-alias testsite \
        -v "${WINGS_ROOT}/tests/e2e/site:/srv:ro" -w /srv docker.io/library/python:3.14.9-slim \
        python -m http.server 8000 >/dev/null
      created="$created ${project}-testsite"
    fi
    ;;
esac
[ -z "$created" ] || sleep 8

bind="$(env_get APP_BIND)"; [ -z "$bind" ] || [ "$bind" = "0.0.0.0" ] && bind=127.0.0.1
port="$(env_get APP_PORT)"; port="${port:-7011}"
origin="$(env_get ALLOWED_ORIGINS | cut -d, -f1)"; origin="${origin:-http://localhost:${port}}"
user="$(env_get ODYSSEUS_ADMIN_USER)"; user="${user:-admin}"
case ",$(env_get COMPOSE_PROFILES)," in
  *,caldav,*)
    dav_port="$(env_get RADICALE_PORT)"
    export WINGS_CALDAV_USER WINGS_CALDAV_PASSWORD WINGS_CALDAV_HOST_URL
    WINGS_CALDAV_USER="$(env_get RADICALE_USER)"
    WINGS_CALDAV_PASSWORD="$(env_get RADICALE_PASSWORD)"
    WINGS_CALDAV_HOST_URL="http://127.0.0.1:${dav_port:-5232}"
    ;;
esac
WINGS_URL="http://${bind}:${port}" \
WINGS_ORIGIN="$origin" \
WINGS_USER="$user" \
WINGS_PASSWORD="$(env_get ODYSSEUS_ADMIN_PASSWORD)" \
COMPOSE_PROJECT_NAME="$project" \
WINGS_APP_CONTAINER="$(container_id odysseus)" \
  python3 "${WINGS_ROOT}/tests/e2e/wings_e2e.py"
