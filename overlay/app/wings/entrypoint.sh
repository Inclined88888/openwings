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
python /app/wings/preflight.py
exec /usr/local/bin/entrypoint.sh "$@"
