"""Unit tests for MCP server usage notes (overlay/app/wings/mcp_notes.py and
the patch 0017 hooks in src/mcp_manager.py).
Run with ./tests/unit/run.sh (inside the built image, network off)."""
from __future__ import annotations

import sys
from types import SimpleNamespace

sys.path.insert(0, "/app")


def test_clean_keeps_text_and_drops_hidden_characters():
    from wings.mcp_notes import clean, MAX_CHARS
    assert clean(None) == "" and clean(42) == ""
    assert clean("Call  search\nfirst,\tthen fetch.") == "Call search first, then fetch."
    assert clean("safe‮txt.exe​ and \x07bell") == "safetxt.exe and bell"
    long = clean("x" * (MAX_CHARS + 50))
    assert len(long) <= MAX_CHARS + 12 and long.endswith("[notes cut]")


def test_notes_appear_with_the_servers_tools():
    from src import mcp_manager
    assert mcp_manager._wings_server_notes(SimpleNamespace(instructions="Use list_repos before get_file.")) \
        == "Use list_repos before get_file."
    assert mcp_manager._wings_server_notes(SimpleNamespace()) == ""
    mgr = mcp_manager.McpManager.__new__(mcp_manager.McpManager)
    mgr._tools = {"gh": [{"name": "list_repos", "description": "List repositories", "input_schema": {}}]}
    mgr._connections = {"gh": {"status": "connected", "name": "GitHub", "instructions": "Use list_repos first."}}
    mgr._generation = 1
    mgr._cached_prompt_desc = None
    mgr._cached_prompt_desc_key = None
    mgr.get_all_tools = lambda disabled_map=None: [{
        "server_id": "gh", "server_name": "GitHub", "name": "list_repos", "qualified_name": "mcp__gh__list_repos",
        "description": "List repositories", "input_schema": {}}]
    mgr.is_builtin = lambda server_id: False
    text = mgr.get_tool_descriptions_for_prompt()
    assert "**GitHub:**" in text and "Use list_repos first." in text
    assert text.index("Use list_repos first.") < text.index("mcp__gh__list_repos")
    assert "not by the user" in text
