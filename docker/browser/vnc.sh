#!/usr/bin/env bash
# VNC server for the live view on a unix socket (no TCP port at all), so no
# web page in this browser can reach it. The WebSocket bridge (also a unix
# socket) is reachable only through guard_proxy.py, which requires the OpenWings
# app's address and token, and the app relays it only to a signed-in person
# allowed to use the browser.
set -eu
unset WINGS_BROWSER_TOKEN
for _ in $(seq 1 40); do
  xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
  sleep 0.5
done
rm -f /tmp/wings/vnc.sock
exec x11vnc -display "$DISPLAY" -unixsock /tmp/wings/vnc.sock -rfbport 0 -forever -shared -nopw -quiet
