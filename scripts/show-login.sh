#!/usr/bin/env bash
# Show where to open OpenWings and how to sign in, from .env. Needs no Docker.
#
#   ./scripts/show-login.sh
#
# The password is the one created at install. The app makes the admin account
# from it on first start only, so if you changed the password in Settings,
# yours is the one that works.
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"
[ -f "$ENV_FILE" ] || die "No .env in ${WINGS_ROOT}: OpenWings is not installed here. Run the installer first."
domain="$(env_get WINGS_DOMAIN)"
if [ -n "$domain" ] && [ "$domain" != "localhost" ]; then url="https://${domain}"; else
  bind_v="$(env_get APP_BIND)"; bind_v="${bind_v:-127.0.0.1}"
  [ "$bind_v" = "0.0.0.0" ] && bind_v="localhost"
  port_v="$(env_get APP_PORT)"; port_v="${port_v:-7011}"
  url="http://${bind_v}:${port_v}"
fi
user_v="$(env_get ODYSSEUS_ADMIN_USER)"; user_v="${user_v:-admin}"
pw_v="$(env_get ODYSSEUS_ADMIN_PASSWORD)"
[ -n "$pw_v" ] || die "No admin password in .env. Run the installer (or ./scripts/setup.sh) to create one."
cat <<OUT

  Open:      ${url}
  Username:  ${user_v}
  Password:  ${pw_v}

  (The password set at install. If you changed it in Settings, use yours.)

OUT
