"""MCP server usage notes (patch 0017).

MCP servers may send `instructions` when a client connects: how their tools
fit together, which to call first, limits. OpenManus passes them to the
model; upstream Odysseus dropped them, so agents used such servers worse.
Wings keeps them, cleaned and capped, and shows them with that server's
tools in the prompt, labelled as the server's words. They are guidance, not
authority: approval gates and tool access rules apply as before.
"""
from __future__ import annotations

import re
import unicodedata

MAX_CHARS = 1500


def clean(text) -> str:
    if not isinstance(text, str):
        return ""
    # Drop control and format characters (including bidi overrides and zero
    # width characters a server could use to hide text), keep line breaks as
    # spaces so the notes stay one line in the catalog.
    kept = []
    for ch in text:
        if ch in "\n\r\t":
            kept.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs"):
            continue
        else:
            kept.append(ch)
    out = re.sub(r"\s+", " ", "".join(kept)).strip()
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS].rstrip() + " [notes cut]"
    return out
