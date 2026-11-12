#!/usr/bin/env bash
# Connect your accounts (patch 0025): end-to-end proof against local stubs.
#
#   ENV_FILE=/path/to/test.env PROJECT=owconnect tests/e2e/accounts_run.sh
#
# Needs a TEST stack started with the stubs override (it adds the stubs
# service and the test-only WINGS_TEST_ENDPOINTS / WINGS_TEST_ALLOW_HOSTS):
#   docker compose --env-file "$ENV_FILE" -p "$PROJECT" -f docker-compose.yml \
#     -f tests/e2e/accounts.stubs.yml up -d --no-build
# Starts the mock model next to it (used only with E2E_CHAT=1), then runs
# accounts_e2e.py on the host.
# Never run against your daily install: it creates accounts and calendars.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${ENV_FILE:?set ENV_FILE to the env file of the test stack}"
PROJECT="${PROJECT:-owconnect}"
net="${PROJECT}_wings"
port="$(grep -E '^APP_PORT=' "$ENV_FILE" | cut -d= -f2)"
user="$(grep -E '^ODYSSEUS_ADMIN_USER=' "$ENV_FILE" | cut -d= -f2)"
pass="$(grep -E '^ODYSSEUS_ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
if [ "$(docker inspect -f '{{.State.Running}}' "${PROJECT}-mockllm" 2>/dev/null)" != "true" ]; then
  docker rm -f "${PROJECT}-mockllm" >/dev/null 2>&1 || true
  docker run -d --name "${PROJECT}-mockllm" --network "$net" --network-alias mockllm \
    -v "$ROOT/tests/e2e:/t:ro" docker.io/library/python:3.14.9-slim python /t/mock_llm.py >/dev/null
fi
WINGS_PORT="$port" WINGS_USER="$user" WINGS_PASSWORD="$pass" WINGS_APP_CONTAINER="${PROJECT}-openwings-1" \
  python3 "$ROOT/tests/e2e/accounts_e2e.py"
