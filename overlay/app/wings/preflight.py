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
from pathlib import Path

from cryptography.fernet import Fernet

ENV_KEY_VAR = "ODYSSEUS_APP_KEY"
KEY_FILES = (".app_key", ".key")
EX_CONFIG = 78


def _truthy_off(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "off"}


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
    if _truthy_off(os.environ.get("WINGS_FORCE_EMAIL_CONFIRM", "1")):
        print(
            "wings: WINGS_FORCE_EMAIL_CONFIRM is off; the in-app setting alone "
            "decides whether agents may send email without your approval.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
