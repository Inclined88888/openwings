"""Keep deployment secrets out of child processes that do not need them.

Upstream passes its whole environment to user-added MCP servers (whenever they
define any env var), to the bundled npx browser server, and to Cookbook
commands. That environment holds ODYSSEUS_APP_KEY (which decrypts every stored
credential), the admin password and provider API keys. Third-party code gets a
copy with those removed; upstream's own Python MCP servers keep the full
environment because they need the key to read encrypted mail credentials.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Mapping

# Exact names that must never reach third-party processes.
ALWAYS_SCRUB = frozenset({
    "ODYSSEUS_APP_KEY",
    "ODYSSEUS_ADMIN_PASSWORD",
    "SEARXNG_SECRET",
    "RADICALE_PASSWORD",
    "DATABASE_URL",
})

# Anything that looks like a credential by name.
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|COOKIE|SESSION)", re.I)

# Credentials some tools legitimately need; kept unless a caller opts out.
_ALLOW_BY_DEFAULT = frozenset({
    "HF_TOKEN",                # Cookbook model downloads
    "HUGGING_FACE_HUB_TOKEN",
})


def is_secret_name(name: str) -> bool:
    return name in ALWAYS_SCRUB or bool(_SECRET_NAME.search(name or ""))


def scrubbed_environ(
    base: Mapping[str, str] | None = None,
    *,
    keep: Iterable[str] = _ALLOW_BY_DEFAULT,
) -> dict[str, str]:
    """Return a copy of the environment without secret-looking variables.

    Variables in ``keep`` survive unless they are in ALWAYS_SCRUB.
    """
    src = dict(os.environ if base is None else base)
    keep = set(keep) - ALWAYS_SCRUB
    return {k: v for k, v in src.items() if k in keep or not is_secret_name(k)}
