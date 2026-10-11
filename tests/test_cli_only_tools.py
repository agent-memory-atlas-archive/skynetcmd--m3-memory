"""ToolSpec.mcp=False: a CLI command, never an MCP tool (U8, user decision
2026-10-11, m3 dcc9f87d). MCP stays read-only for the user's config."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def test_chatlog_set_redaction_is_cli_only():
    import mcp_tool_catalog as cat

    spec = cat.get_tool("chatlog_set_redaction")
    assert spec is not None and spec.mcp is False and spec.default_allowed is False
    assert spec in cat.TOOLS, "the CLI keeps every tool"
    assert "chatlog_set_redaction" not in {t.name for t in cat.MCP_TOOLS}


def test_m3_call_refuses_a_cli_only_tool():
    import mcp_tool_catalog as cat

    out = json.loads(asyncio.run(cat.m3_call_impl(tool="chatlog_set_redaction", args={"enabled": False})))
    assert out["ok"] is False and out["error"] == "cli_only"


def test_no_mcp_surface_lists_a_cli_only_tool():
    import mcp_tool_catalog as cat

    cli_only = {t.name for t in cat.TOOLS if not t.mcp}
    manifest = json.loads((Path(__file__).resolve().parents[1] / "docs/tools/MCP_CATALOG.json").read_text("utf-8"))
    assert not cli_only & {t["name"] for t in manifest["tools"]}
