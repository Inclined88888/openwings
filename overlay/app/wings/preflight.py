"""Wings preflight: fail fast on encryption-key misconfiguration.

Runs as the container starts, before Odysseus itself, so a bad
ODYSSEUS_APP_KEY stops the container with a clear message instead of
surfacing later as secrets that silently decrypt to nothing.
Exit code 78 (EX_CONFIG) means the configuration must be fixed.

Deliberately imports nothing from the application: importing the upstream
`src` package runs database migrations as a side effect. The checks below
mirror src/secret_storage.py (patch 0001) and must stay in sync with it.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from cryptography.fernet import Fernet

ENV_KEY_VAR = "ODYSSEUS_APP_KEY"
KEY_FILES = (".app_key", ".key")
EX_CONFIG = 78


def _truthy_off(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "off"}


# (table, column) pairs that upstream encrypts with the app key (enc: prefix).
_ENCRYPTED_COLUMNS = (
    ("model_endpoints", "api_key"),
    ("email_accounts", "imap_password"),
    ("email_accounts", "smtp_password"),
    ("email_accounts", "oauth_refresh_token"),
    ("mcp_servers", "env"),
)


def _valid_fernet(key: bytes) -> bool:
    try:
        Fernet(key)
        return True
    except (ValueError, TypeError):
        return False


def _key_fails_existing_data(data_dir: Path, key: bytes, require_sample: bool = False) -> str:
    """Return a description of the first stored secret the key cannot decrypt.

    Reads app.db read-only and immutable (no locks, no -shm/-wal writes), so it
    is safe while running as root before privileges drop. Any error reading
    the database is not treated as a key problem. With require_sample=True,
    finding no encrypted value at all counts as "cannot tell" (a failure), so
    callers never treat an empty database as proof that a key is right."""
    import sqlite3
    from cryptography.fernet import InvalidToken

    db = data_dir / "app.db"
    if not db.exists():
        return "no database" if require_sample else ""
    fernet = Fernet(key)
    checked = 0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error:
        return "unreadable database" if require_sample else ""
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column in _ENCRYPTED_COLUMNS:
            if table not in tables:
                continue
            try:
                row = conn.execute(
                    f"SELECT {column} FROM {table} WHERE {column} LIKE 'enc:%' LIMIT 1"
                ).fetchone()
            except sqlite3.Error:
                continue
            if not row:
                continue
            try:
                fernet.decrypt(row[0][4:].encode("ascii"))
                checked += 1
            except (InvalidToken, ValueError, UnicodeEncodeError):
                return f"{table}.{column}"
    except sqlite3.Error:
        return "unreadable database" if require_sample else ""
    finally:
        conn.close()
    if require_sample and not checked:
        return "no encrypted values to test"
    return ""


def _migrate_scheduled_emails(data_dir: Path) -> None:
    """First start of an upstream volume under Wings: keep pending sends that
    a person scheduled with upstream's UI (patch 0004 flag column)."""
    import sqlite3

    db = data_dir / "scheduled_emails.db"
    if not db.exists():
        return
    sys.path.insert(0, "/app")
    from wings.email_gate import ensure_scheduled_flag

    try:
        conn = sqlite3.connect(str(db))
        try:
            ensure_scheduled_flag(conn, approve_existing=True)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"wings: could not migrate scheduled emails: {exc}", file=sys.stderr)


def main() -> int:
    data_dir = Path(os.environ.get("ODYSSEUS_DATA_DIR") or "/app/data")
    raw = os.environ.get(ENV_KEY_VAR, "").strip()
    if not raw:
        print(
            f"wings: {ENV_KEY_VAR} is not set; encryption keys will be kept inside "
            "the data volume (upstream default). Run ./scripts/setup.sh to move "
            "the key into .env.",
            file=sys.stderr,
        )
        return 0
    try:
        key = raw.encode("ascii")
        Fernet(key)
    except (UnicodeEncodeError, ValueError, TypeError):
        print(
            f"wings: configuration error: {ENV_KEY_VAR} is not a valid Fernet key "
            "(expected 44 URL-safe base64 characters).",
            file=sys.stderr,
        )
        return EX_CONFIG
    for name in KEY_FILES:
        path = data_dir / name
        try:
            file_key = path.read_bytes().strip()
        except FileNotFoundError:
            continue
        if file_key and file_key != key:
            env_ok = not _key_fails_existing_data(data_dir, key, require_sample=True)
            file_ok = _valid_fernet(file_key) and not _key_fails_existing_data(data_dir, file_key, require_sample=True)
            if env_ok and not file_ok:
                # A stray key minted by some process that lacked the env key:
                # the configured key provably decrypts the stored data, so
                # quarantine the file instead of refusing to start.
                stray = path.with_name(f"{name}.stray-{int(time.time())}")
                path.rename(stray)
                print(f"wings: moved stray key file {path} to {stray}; ODYSSEUS_APP_KEY "
                      "decrypts the stored data.", file=sys.stderr)
                continue
            print(
                f"wings: configuration error: {path} exists and differs from "
                f"{ENV_KEY_VAR}. Secrets stored so far were encrypted with the key "
                "in that file. Run ./scripts/adopt-key.sh --yes to move it into "
                ".env, or unset the variable.",
                file=sys.stderr,
            )
            return EX_CONFIG
        print(
            f"wings: {path} holds the same key as {ENV_KEY_VAR}. Delete it to keep "
            "the key out of the data volume and its backups.",
            file=sys.stderr,
        )
    problem = _key_fails_existing_data(data_dir, key)
    if problem and os.environ.get("WINGS_ACCEPT_KEY_LOSS", "").strip() == "1":
        print(
            f"wings: WARNING: {ENV_KEY_VAR} does not decrypt {problem}; starting anyway "
            "because WINGS_ACCEPT_KEY_LOSS=1. Stored credentials are unreadable; re-enter "
            "them in Settings, then remove WINGS_ACCEPT_KEY_LOSS from .env.",
            file=sys.stderr,
        )
        problem = ""
    if problem:
        print(
            f"wings: configuration error: {ENV_KEY_VAR} does not decrypt data already "
            f"stored in this volume ({problem}). This .env belongs to a different install "
            "or its key was regenerated. Restore the matching .env. To start anyway with "
            "stored credentials unreadable, set WINGS_ACCEPT_KEY_LOSS=1 in .env.",
            file=sys.stderr,
        )
        return EX_CONFIG
    _migrate_scheduled_emails(data_dir)
    if _truthy_off(os.environ.get("WINGS_FORCE_EMAIL_CONFIRM", "1")):
        print(
            "wings: WINGS_FORCE_EMAIL_CONFIRM is off; the in-app setting alone "
            "decides whether agents may send email without your approval.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
