"""The MCP startup tool set has exactly ONE owner: tools_config.

Until 2026-10-17 there were three: tools_config.DEFAULT_STARTUP_TOOLS (what
`m3 doctor` reported), tool_domains.ESSENTIAL_TOOL_NAMES plus
memory_bridge._META_TOOLS (what the server registered). They disagreed, and a
user's tools.startup / .tools_config.json reached the doctor's report and not
the server. These tests keep one owner and keep every reader on it.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

_BIN = os.path.join(os.path.dirname(__file__), "..", "bin")


def test_essentials_are_derived_from_the_owner():
    import tool_domains
    import tools_config

    assert tool_domains.ESSENTIAL_TOOL_NAMES == frozenset(tools_config.DEFAULT_STARTUP_TOOLS)


def test_the_shipped_default_carries_the_escape_hatch_and_real_tools():
    import mcp_tool_catalog
    import tools_config

    assert set(tools_config.ESCAPE_HATCH) <= set(tools_config.DEFAULT_STARTUP_TOOLS)
    tools_config.validate_against_catalog(
        list(tools_config.DEFAULT_STARTUP_TOOLS), {s.name for s in mcp_tool_catalog.MCP_TOOLS}
    )


def test_the_bridge_registers_what_the_owner_resolves():
    import memory_bridge
    import tools_config

    names, _source = tools_config.resolve_startup_tools()
    expected = None if names is None else frozenset(names)
    assert memory_bridge.STARTUP_TOOLS == expected


def test_no_second_literal_list_of_startup_tools():
    """A hand-copied list of the set elsewhere in bin/ is the drift this replaced."""
    pattern = re.compile(r"_META_TOOLS\s*=|ESSENTIAL_TOOL_NAMES[^=\n]*=\s*frozenset\(\s*\{")
    offenders = []
    for root, _dirs, files in os.walk(_BIN):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    if pattern.search(fh.read()):
                        offenders.append(os.path.relpath(path, _BIN))
    assert not offenders, f"a second startup-set literal: {offenders}"
