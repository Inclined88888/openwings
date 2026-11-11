#!/usr/bin/env bash
# Entry point of the OpenWings browser container. Runs the virtual screen, VNC,
# noVNC, Chromium and the guard proxy under supervisord (restarted on crash).
set -euo pipefail

if [ -n "${VIRTUAL_ENV:-}" ] && [ -f "${VIRTUAL_ENV}/bin/activate" ]; then
  # shellcheck disable=SC1091
  . "${VIRTUAL_ENV}/bin/activate"
fi

# Screen size. WINGS_BROWSER_SCREEN=WIDTHxHEIGHT overrides the image default.
if [ -n "${WINGS_BROWSER_SCREEN:-}" ]; then
  case "$WINGS_BROWSER_SCREEN" in
    [0-9]*x[0-9]*)
      export SE_SCREEN_WIDTH="${WINGS_BROWSER_SCREEN%%x*}"
      export SE_SCREEN_HEIGHT="${WINGS_BROWSER_SCREEN##*x}"
      ;;
    *) echo "WINGS_BROWSER_SCREEN must look like 1440x900, ignoring '$WINGS_BROWSER_SCREEN'" >&2 ;;
  esac
fi

mkdir -p /tmp/wings
exec supervisord --configuration /opt/wings/supervisord.conf
