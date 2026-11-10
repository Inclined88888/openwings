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
# Three phases, so a failure at any point loses nothing:
#   1. In a throwaway container: check that the key in data/.app_key decrypts
#      the data already stored, and re-encrypt what upstream protected with its
#      second key (data/.key: webhook signing secrets and api_keys.json) to it.
#      Nothing is deleted.
#   2. Write the key to .env (the previous value is kept as a comment) and
#      read it back.
#   3. Only then delete data/.app_key and data/.key from the volume.
# Take a backup first (./scripts/backup.sh).
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

# Phase 1 ---------------------------------------------------------------------
key="$(docker run --rm -i --network none --entrypoint python -v "${vol}:/data" "$IMAGE" - <<'PY'
import json, os, sqlite3, sys, tempfile
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

d = Path("/data")
app_key_file, legacy_key_file = d / ".app_key", d / ".key"
api_keys, app_db = d / "api_keys.json", d / "app.db"
if not app_key_file.exists() and not legacy_key_file.exists():
    sys.exit("no key files in the data volume; nothing to adopt")
key = (app_key_file.read_bytes() if app_key_file.exists() else legacy_key_file.read_bytes()).strip()
new = Fernet(key)

# The adopted key must decrypt what is already stored with the enc: prefix.
if app_db.exists():
    conn = sqlite3.connect(str(app_db))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in (("model_endpoints", "api_key"), ("email_accounts", "imap_password"),
                              ("email_accounts", "smtp_password"), ("mcp_servers", "env")):
            if table not in tables:
                continue
            row = conn.execute(f"SELECT {column} FROM {table} WHERE {column} LIKE 'enc:%' LIMIT 1").fetchone()
            if row:
                try:
                    new.decrypt(row[0][4:].encode())
                except InvalidToken:
                    sys.exit(f"data/.app_key does not decrypt {table}.{column}; refusing to adopt it")
    finally:
        conn.close()

# Values upstream encrypted with its second key (data/.key) move to the adopted key.
if legacy_key_file.exists() and legacy_key_file.read_bytes().strip() != key:
    old = Fernet(legacy_key_file.read_bytes().strip())

    def rekey(token):
        if not isinstance(token, str) or not token:
            return token
        try:
            return new.encrypt(old.decrypt(token.encode())).decode()
        except (InvalidToken, ValueError):
            return token  # plaintext legacy value, or already under the new key

    if api_keys.exists():
        try:
            data = json.loads(api_keys.read_text(encoding="utf-8") or "{}")
        except ValueError:
            data = None
        if isinstance(data, dict):
            out = {k: rekey(v) for k, v in data.items()}
            fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".api_keys.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(out, fh)
            os.replace(tmp, api_keys)
        else:
            print("warning: api_keys.json is not a JSON object; left unchanged", file=sys.stderr)
    if app_db.exists():
        conn = sqlite3.connect(str(app_db))
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "webhooks" in tables:
                with conn:
                    for wid, secret in conn.execute("SELECT id, secret FROM webhooks").fetchall():
                        new_secret = rekey(secret)
                        if new_secret != secret:
                            conn.execute("UPDATE webhooks SET secret = ? WHERE id = ?", (new_secret, wid))
        finally:
            conn.close()
print(key.decode())
PY
)"
[ -n "$key" ] || die "no key was read; nothing changed"

# Phase 2 ---------------------------------------------------------------------
previous="$(env_get ODYSSEUS_APP_KEY)"
if [ -n "$previous" ] && [ "$previous" != "$key" ]; then
  printf '# ODYSSEUS_APP_KEY replaced by adopt-key.sh on %s; previous value: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$previous" >> "$ENV_FILE"
fi
env_set ODYSSEUS_APP_KEY "$key"
[ "$(env_get ODYSSEUS_APP_KEY)" = "$key" ] || die ".env could not be updated; the key files are still in the volume, nothing was lost"
ok "ODYSSEUS_APP_KEY in .env now holds the adopted key"

# Phase 3 ---------------------------------------------------------------------
docker run --rm --network none --entrypoint sh -v "${vol}:/data" "$IMAGE" -c 'rm -f /data/.app_key /data/.key'
ok "key files removed from the volume"

dc up -d odysseus
if wait_healthy odysseus 600; then ok "Odysseus is healthy"; else die "Odysseus did not become healthy; see: docker compose logs odysseus"; fi
warn "Update your offline copy of .env now: the key changed."
