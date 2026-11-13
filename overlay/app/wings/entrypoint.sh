#!/bin/sh
# OpenWings entrypoint: run the preflight checks, then hand over to the base
# image's entrypoint (which fixes volume ownership and drops privileges).
#
# The container is configured with OPENWINGS_* settings only. Every Python
# process maps them to the names the upstream code reads by itself
# (sitecustomize, from wings/site_names.py), so this script passes the
# environment on unchanged.
set -e
# The app user is called openwings. For a PUID/PGID that has no user or group
# yet, add it here under that name (the base entrypoint would otherwise add
# one under its own name); the image's own user keeps its files.
if [ "$(id -u)" = 0 ]; then
  PUID="${PUID:-1000}"; PGID="${PGID:-1000}"
  if ! getent group "$PGID" >/dev/null 2>&1; then
    if getent group openwings >/dev/null 2>&1; then groupmod -n openwings-image openwings; fi
    groupadd -g "$PGID" openwings
  fi
  if ! getent passwd "$PUID" >/dev/null 2>&1; then
    if getent passwd openwings >/dev/null 2>&1; then usermod -l openwings-image openwings; fi
    useradd -u "$PUID" -g "$PGID" -M -s /bin/sh -d /app openwings
  fi
fi
# Sign in off (OPENWINGS_SIGN_IN=off in .env, patch 0029): only when the app
# port is published on this computer alone and the HTTPS proxy is not in use.
# Accounts stay on; wings/local_guard.py refuses everything but the app's own
# page in a browser on this computer and signs that browser in as the admin.
case "$(printf '%s' "${OPENWINGS_SIGN_IN:-on}" | tr '[:upper:]' '[:lower:]')" in
  off|false|no|0)
    case ",$(printf '%s' "${OPENWINGS_PROFILES:-}" | tr -d ' ')," in
      *,https,*)
        echo "OpenWings: OPENWINGS_SIGN_IN=off is ignored because the HTTPS profile puts OpenWings online. Sign in stays on." >&2
        ;;
      *)
        case "${OPENWINGS_APP_BIND:-}" in
          127.0.0.1|localhost|::1|'[::1]')
            export WINGS_SIGN_IN_OFF=1
            echo "OpenWings: sign in is off. A browser on this computer opens OpenWings without signing in." >&2
            ;;
          *)
            echo "OpenWings: OPENWINGS_SIGN_IN=off is ignored because the app is reachable from other computers (APP_BIND=${OPENWINGS_APP_BIND:-unset}). Sign in stays on." >&2
            ;;
        esac
        ;;
    esac
    ;;
esac
unset OPENWINGS_SIGN_IN OPENWINGS_APP_BIND OPENWINGS_PROFILES
python /app/wings/preflight.py
exec /usr/local/bin/entrypoint.sh "$@"
