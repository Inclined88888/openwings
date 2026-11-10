"""
secret_storage.py

Fernet-based symmetric encryption for secrets stored in the SQLite DB
(IMAP / SMTP passwords today; safe to extend). The key lives at
`data/.app_key`, mode 0o600, generated on first call. `data/` is
gitignored so the key never ships with the repo.

Threat model: protects against SQLite-file exfiltration (stolen
backup, leaked container layer, sibling-tenant read). Does **not**
protect against a process compromise — anyone who can read this
module's memory or the key file has plaintext.

Wings: when ODYSSEUS_APP_KEY is set (a Fernet key from the deployment's
environment), that key is used instead and no key file is ever written, so
the key does not travel with the data directory or its backups. If a key
file already exists and holds a different key, startup fails loudly rather
than silently making every stored secret undecryptable.

Encrypted values carry an `enc:` prefix so the migration is
idempotent: passing an already-encrypted value to `encrypt()` is a
no-op; passing a plaintext value to `decrypt()` returns it
unchanged. That lets legacy rows coexist with new ones until a
single migration pass rewrites them.
"""

import os
import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from core.platform_compat import safe_chmod
from src.constants import APP_KEY_FILE

logger = logging.getLogger(__name__)

_KEY_PATH = Path(APP_KEY_FILE)
_PREFIX = "enc:"
_fernet: Fernet | None = None


ENV_KEY_VAR = "ODYSSEUS_APP_KEY"


def env_app_key() -> bytes | None:
    """Return the Fernet key from ODYSSEUS_APP_KEY, or None when unset.

    Raises RuntimeError for a malformed key so a typo never falls back to a
    freshly generated key (which would orphan every stored secret)."""
    raw = os.environ.get(ENV_KEY_VAR, "").strip()
    if not raw:
        return None
    key = raw.encode("ascii", errors="strict") if raw.isascii() else b""
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{ENV_KEY_VAR} is not a valid Fernet key (expected 44 URL-safe "
            "base64 characters, e.g. from `python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\"`)"
        ) from exc
    return key


def check_key_file_matches_env(path: Path, env_key: bytes) -> None:
    """Refuse to run when an on-disk key differs from the environment key.

    Values already stored were encrypted with the file key; switching keys
    silently would make them decrypt to "" (unconfigured) everywhere."""
    try:
        file_key = path.read_bytes().strip()
    except FileNotFoundError:
        return
    if file_key and file_key != env_key:
        raise RuntimeError(
            f"{path} exists and differs from {ENV_KEY_VAR}. Secrets stored so "
            f"far were encrypted with the key in that file. Move it into the "
            f"environment (Wings: ./scripts/adopt-key.sh --yes), or unset "
            f"{ENV_KEY_VAR}."
        )


def _load_or_create_key() -> bytes:
    env_key = env_app_key()
    if env_key is not None:
        check_key_file_matches_env(_KEY_PATH, env_key)
        return env_key
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes()
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    _KEY_PATH.write_bytes(key)
    # POSIX: lock the key to 0o600. Windows: no-op (the user-profile data dir is
    # already ACL-restricted); safe_chmod swallows both cases.
    safe_chmod(_KEY_PATH, 0o600)
    logger.info(f"Generated new app key at {_KEY_PATH}")
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Empty input passes through. Already-encrypted
    values pass through unchanged so re-encrypting is a no-op."""
    if not plaintext:
        return plaintext or ""
    if plaintext.startswith(_PREFIX):
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value: str) -> str:
    """Decrypt an `enc:`-prefixed value. Plaintext (legacy) passes
    through unchanged. Returns "" on decryption failure so a corrupt
    or rotated-key row degrades to "unconfigured" rather than 500."""
    if not value:
        return value or ""
    if not value.startswith(_PREFIX):
        return value
    try:
        return _get_fernet().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt stored secret — wrong key or corrupt token")
        return ""
    except Exception as e:
        logger.error(f"Decrypt failure: {e}")
        return ""


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_PREFIX)
