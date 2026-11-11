"""Holds the encryption key in memory instead of the process environment.

wings.serve moves ODYSSEUS_APP_KEY from os.environ into APP_KEY before the
app starts, so no child process the app spawns (Cookbook engines in tmux,
scheduled scripts, user-added MCP servers, terminals) inherits it. Upstream's
own MCP servers, which need the key, receive it explicitly through
with_key().
"""
from __future__ import annotations

APP_KEY: str | None = None
# Shared secret the browser container's guard requires on every connection
# (WINGS_BROWSER_TOKEN); held here for the same reason as APP_KEY.
BROWSER_TOKEN: str | None = None
# Same for the code runner container (WINGS_RUNNER_TOKEN).
RUNNER_TOKEN: str | None = None
# Same for the stateless page renderer container (WINGS_RENDERER_TOKEN).
RENDERER_TOKEN: str | None = None


def with_key(env: dict) -> dict:
    """Return env plus ODYSSEUS_APP_KEY when the key is held here."""
    if APP_KEY:
        return {**env, "ODYSSEUS_APP_KEY": APP_KEY}
    return env
