"""Per-agent tool access for Odysseus personas (Wings patch 0005).

Upstream "agents" are persona presets (instructions, temperature, persona
memory) with no tool restriction of their own. Wings adds an optional
``enabled_tools`` list to each persona: a list of tool *families* (below). When
it is set, every tool outside those families is added to the turn's
disabled-tool set, the same choke point upstream uses for the global admin
denylist, so the restriction is enforced server-side, not by prompt.

``enabled_tools`` semantics:
  * missing / None  -> no restriction (upstream behaviour)
  * []              -> no tools at all (pure chat agent)
  * ["web", ...]    -> only those families (plus a tiny always-on core)
  * anything else   -> treated as [] (fail closed)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# UI families -> upstream turn_contract families (src/turn_contract.py).
FAMILIES: dict[str, dict] = {
    "web": {"label": "Web search and reading", "contract": ["search_browser"]},
    "notes": {"label": "Notes", "contract": ["notes"]},
    "tasks": {"label": "Tasks and schedules", "contract": ["tasks"]},
    "calendar": {"label": "Calendar", "contract": ["calendar"]},
    "email": {"label": "Email (sending always needs your click)", "contract": ["email"]},
    "contacts": {"label": "Contacts", "contract": ["contacts"]},
    "documents": {"label": "Documents", "contract": ["documents"]},
    "memory": {"label": "Memory, skills and past chats", "contract": ["memory", "skills"], "mcp": ["memory"]},
    "research": {"label": "Deep research", "contract": ["research"]},
    "files": {
        "label": "Read files and media",
        "contract": ["ocr", "media_inspection", "transcription"],
        "tools": ["read_file", "ls", "glob", "grep", "get_workspace"],
        "mcp": ["rag"],
    },
    "images": {"label": "Image generation and editing", "contract": ["image_generation", "image_editing"], "mcp": ["image_gen"]},
    "shell": {"label": "Shell, code and file writes (admin)", "contract": ["shell_files"]},
    "sessions": {"label": "Other chats and models", "contract": ["sessions"]},
    "admin": {"label": "Admin, models and settings", "contract": ["cookbook_admin"]},
    "external_mcp": {"label": "MCP servers you added", "external_mcp": True},
}

# Always available: these only talk to the person in the chat or drive the
# person's own UI (opening panels, toggles), never external systems.
ALWAYS_ALLOWED = frozenset({"ask_user", "update_plan", "todowrite", "ui_control"})

# Contact editing belongs to the Contacts family even though upstream lists it
# under email too; email keeps read-only address lookup.
CONTACT_WRITE_TOOLS = frozenset({"manage_contact"})

BUILTIN_MCP_SERVERS = frozenset({"email", "memory", "rag", "image_gen"})


def _data_dir() -> Path:
    return Path(os.environ.get("ODYSSEUS_DATA_DIR") or "/app/data")


def _load_presets() -> dict:
    try:
        data = json.loads((_data_dir() / "presets.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalize(allowed) -> list | None:
    if allowed is None:
        return None
    if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
        return []  # malformed: fail closed
    return allowed


def load_template_allowlist(template_id: str | None) -> list | None:
    """enabled_tools of a saved agent template (group chat participants)."""
    if not template_id:
        return None
    for t in _load_presets().get("user_templates") or []:
        if isinstance(t, dict) and t.get("id") == template_id:
            return _normalize(t.get("enabled_tools"))
    return None


def load_active_allowlist(preset_id: str | None) -> list | None:
    """Return the active persona's enabled_tools, or None for no restriction."""
    if not preset_id:
        return None
    presets = _load_presets()
    preset = presets.get(preset_id)
    if not isinstance(preset, dict):
        return None
    if preset_id == "custom" and not preset.get("enabled", True):
        return None
    return _normalize(preset.get("enabled_tools"))


def _contract_families() -> dict:
    try:
        from src.turn_contract import FAMILY_TOOLS
        return dict(FAMILY_TOOLS)
    except Exception:
        return {}


def _mcp_tools() -> list[tuple[str, str]]:
    """(server_id, tool_name) for every connected MCP tool."""
    try:
        from src.tool_utils import get_mcp_manager
        mgr = get_mcp_manager()
        if not mgr:
            return []
        return [(t["server_id"], t["name"]) for t in mgr.get_all_tools()]
    except Exception:
        return []


def _universe(contract: dict, mcp: list[tuple[str, str]]) -> set[str]:
    names: set[str] = set()
    for tools in contract.values():
        names.update(tools)
    try:
        from src.tool_types import TOOL_TAGS
        names.update(TOOL_TAGS)
    except Exception:
        pass
    try:
        from src.tool_policy import known_tool_names
        names.update(known_tool_names())
    except Exception:
        pass
    for server, tool in mcp:
        names.add(f"mcp__{server}__{tool}")
    return names


def allowed_tool_names(families: list[str], contract: dict, mcp: list[tuple[str, str]]) -> set[str]:
    allowed = set(ALWAYS_ALLOWED)
    for fam in families:
        spec = FAMILIES.get(fam)
        if not spec:
            continue
        for cfam in spec.get("contract", ()):
            allowed.update(contract.get(cfam, ()))
        allowed.update(spec.get("tools", ()))
        for server, tool in mcp:
            if server in spec.get("mcp", ()):
                allowed.add(f"mcp__{server}__{tool}")
            if spec.get("external_mcp") and server not in BUILTIN_MCP_SERVERS:
                allowed.add(f"mcp__{server}__{tool}")
    if "contacts" not in families:
        allowed -= CONTACT_WRITE_TOOLS
    # The built-in email MCP server exposes the email family's tools.
    for server, tool in mcp:
        if server == "email" and tool in allowed:
            allowed.add(f"mcp__{server}__{tool}")
    return allowed


def agent_denied_tools(preset_id: str | None, template_id: str | None = None) -> set[str]:
    """Tool names to add to the turn's disabled set.

    Combines the active persona (preset_id, normal chats) and an explicit
    saved agent template (template_id, group chat participants): a tool must
    be allowed by every restriction that applies."""
    restrictions = [r for r in (load_active_allowlist(preset_id), load_template_allowlist(template_id)) if r is not None]
    if not restrictions:
        return set()
    contract = _contract_families()
    mcp = _mcp_tools()
    universe = _universe(contract, mcp)
    denied: set[str] = set()
    for families in restrictions:
        denied |= universe - allowed_tool_names(families, contract, mcp)
    return denied


def families_for_ui() -> list[dict]:
    return [{"id": k, "label": v["label"]} for k, v in FAMILIES.items()]
