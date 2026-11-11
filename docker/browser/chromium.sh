#!/usr/bin/env bash
# Starts Chromium on the virtual screen with a persistent profile, so logins
# you make in the live view survive restarts. DevTools listens on loopback
# only; guard_proxy.py is the single way in, and it admits only the OpenWings app.
# Every request Chromium makes goes through egress_proxy.py (no bypass, not
# even for loopback), which refuses private and local addresses.
set -eu
unset WINGS_BROWSER_TOKEN

PROFILE=/home/seluser/wings-profile
mkdir -p "$PROFILE"
# A container restart leaves lock files naming the old hostname, and Chromium
# would then refuse the profile as "in use on another computer".
rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonSocket" "$PROFILE/SingletonCookie"

for _ in $(seq 1 40); do
  xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
  sleep 0.5
done

# /usr/bin/chromium is Selenium's wrapper, which adds --no-sandbox: Chromium's
# own sandbox needs user namespaces that Docker's default seccomp profile
# blocks. The container is the isolation boundary (no app data or secrets in
# it, all capabilities dropped, no-new-privileges).
exec /usr/bin/chromium \
  --user-data-dir="$PROFILE" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --proxy-server=http://127.0.0.1:3128 \
  --proxy-bypass-list="<-loopback>" \
  --no-first-run \
  --no-default-browser-check \
  --password-store=basic \
  --disable-features=Translate,MediaRouter \
  --hide-crash-restore-bubble \
  --window-position=0,0 \
  --window-size="${SE_SCREEN_WIDTH},${SE_SCREEN_HEIGHT}" \
  --start-maximized \
  --lang="${WINGS_BROWSER_LANG:-en-US}" \
  about:blank
