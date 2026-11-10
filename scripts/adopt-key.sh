#!/usr/bin/env bash
# Move an encryption key that already lives in the data volume into .env.
#
# Needed only when data was created without ODYSSEUS_APP_KEY (an upstream
# Odysseus install, a restored upstream backup, or a stack started before
# setup.sh). Symptom: the odysseus container restarts with
#   "wings: configuration error: /app/data/.app_key exists and differs ..."
#
#   ./scripts/adopt-key.sh --yes
#
# What it does: reads data/.app_key, re-encrypts data/api_keys.json from
# data/.key to that key if the two differ, writes the key to .env as
# ODYSSEUS_APP_KEY (keeping the previous value as a comment), and deletes the
# key files from the volume. Take a backup first (./scripts/backup.sh).
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

[ "${1:-}" = "--yes" ] || die "this rewrites .env and deletes key files from the data volume. Re-run with --yes."
require_docker
[ -f "$ENV_FILE" ] || die "no .env found"
vol="$(volume_name odysseus-data)"
[ -n "$vol" ] || die "the odysseus-data volume does not exist"
IMAGE="$(wings_image)"

info "Stopping odysseus"
dc stop odysseus >/dev/null 2>&1 || true

key="$(docker run --rm -i --network none --entrypoint python -v "${vol}:/data" "$IMAGE" - <<'PY'
import json, os, sys
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
d = Path("/data")
app_key_file, legacy_key_file, api_keys = d / ".app_key", d / ".key", d / "api_keys.json"
if not app_key_file.exists() and not legacy_key_file.exists():
    sys.exit("no key files in the data volume; nothing to adopt")
key = (app_key_file.read_bytes() if app_key_file.exists() else legacy_key_file.read_bytes()).strip()
Fernet(key)
if legacy_key_file.exists() and api_keys.exists():
    old = Fernet(legacy_key_file.read_bytes().strip())
    new = Fernet(key)
    data = json.loads(api_keys.read_text(encoding="utf-8") or "{}")
    out = {}
    for provider, token in data.items():
        try:
            out[provider] = new.encrypt(old.decrypt(token.encode())).decode()
        except InvalidToken:
            out[provider] = token
            print(f"warning: could not re-encrypt api key for {provider}", file=sys.stderr)
    tmp = api_keys.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out), encoding="utf-8")
    os.replace(tmp, api_keys)
for f in (app_key_file, legacy_key_file):
    if f.exists():
        f.unlink()
print(key.decode())
PY
)"

[ -n "$key" ] || die "no key was read; .env left unchanged"
previous="$(env_get ODYSSEUS_APP_KEY)"
if [ -n "$previous" ] && [ "$previous" != "$key" ]; then
  printf '# ODYSSEUS_APP_KEY replaced by adopt-key.sh on %s; previous value: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$previous" >> "$ENV_FILE"
fi
env_set ODYSSEUS_APP_KEY "$key"
ok "ODYSSEUS_APP_KEY in .env now holds the adopted key; key files removed from the volume"

dc up -d odysseus
if wait_healthy odysseus 600; then ok "Odysseus is healthy"; else die "Odysseus did not become healthy; see: docker compose logs odysseus"; fi
warn "Update your offline copy of .env now: the key changed."
