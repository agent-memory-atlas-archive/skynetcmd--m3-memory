"""In-process unit tests for mcp_proxy: catalog gating and dispatch.

Verifies that the proxy imports cleanly, serves only m3's own catalog, hides
destructive catalog tools by default and exposes them when allowed, and that
_execute_tool refuses unknown and disabled tools with a helpful message.

This file lived in bin/ and was run by hand only, so it rotted: it asserted the
deleted protocol and debug bridges' 5 and 6 tools long after they were gone.
It now runs with the suite. The gate is set on the proxy module directly rather
than through the environment: what is tested here is the proxy's gating, and
`destructive_tools_allowed` (the env reading) has its own owner in m3_sdk.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

_DESTRUCTIVE = {
    "memory_delete",
    "memory_maintenance",
    "memory_set_retention",
    "memory_export",
    "memory_import",
    "gdpr_export",
    "gdpr_forget",
    "agent_offline",
}


def _proxy(monkeypatch, *, allow: bool):
    mp = importlib.import_module("mcp_proxy")
    monkeypatch.setattr(mp, "ALLOW_DESTRUCTIVE", allow)
    # A fresh, empty catalog cache, so the build below reflects `allow`.
    monkeypatch.setattr(mp, "_catalog_built", False)
    monkeypatch.setattr(mp, "_catalog_tool_schemas", [])
    monkeypatch.setattr(mp, "_catalog_specs_by_name", {})
    return mp


@pytest.fixture
def proxy(monkeypatch):
    return _proxy(monkeypatch, allow=False)


@pytest.fixture
def proxy_allowing_destructive(monkeypatch):
    return _proxy(monkeypatch, allow=True)


def _names(schemas) -> set[str]:
    return {t["function"]["name"] for t in schemas}


def test_imports_clean(proxy):
    assert hasattr(proxy, "PROXY_HOST")
    assert hasattr(proxy, "LMSTUDIO_BASE")
    assert proxy.PROXY_PORT == 9000


def test_the_deleted_bridges_left_no_tools_behind(proxy):
    """The protocol and debug bridges were deleted; the proxy serves only m3's catalog."""
    assert proxy.PROTOCOL_TOOLS == []
    assert proxy.DEBUG_TOOLS == []


def test_catalog_default_filters_destructive(proxy):
    leaked = _names(proxy._build_catalog_tools()[0]) & _DESTRUCTIVE
    assert leaked == set(), f"Destructive tools leaked into default mode: {leaked}"


def test_total_tool_count_default(proxy):
    catalog_schemas, _ = proxy._build_catalog_tools()
    total = len(proxy.PROTOCOL_TOOLS) + len(proxy.DEBUG_TOOLS) + len(catalog_schemas)
    assert total == len(proxy.get_mcp_tools())
    assert total >= 40


def test_execute_unknown_tool(proxy):
    assert "Unknown MCP tool" in asyncio.run(proxy._execute_tool("nope_not_real", {}, "tester"))


def test_execute_destructive_tool_default_denied(proxy):
    result = asyncio.run(proxy._execute_tool("memory_delete", {"id": "x"}, "tester"))
    assert "destructive and disabled by default" in result
    assert "M3_MCP_PROXY_ALLOW_DESTRUCTIVE" in result, "the hint must name the current variable"


def test_execute_catalog_tool_agent_list(proxy):
    result = asyncio.run(proxy._execute_tool("agent_list", {}, "test-smoke"))
    assert isinstance(result, str)
    assert result.startswith("Agents (") or "agents" in result.lower(), result[:200]


def test_inject_agent_id_on_memory_write():
    cat = importlib.import_module("mcp_tool_catalog")
    spec = cat.get_tool("memory_write")
    assert spec is not None
    assert spec.inject_agent_id, "memory_write must have inject_agent_id=True"


def test_legacy_dispatch_table_complete(proxy):
    legacy = _names(proxy.PROTOCOL_TOOLS) | _names(proxy.DEBUG_TOOLS)
    missing = legacy - set(proxy._LEGACY_DISPATCH.keys())
    assert missing == set(), f"Legacy tools missing from _LEGACY_DISPATCH: {missing}"


def test_destructive_exposed(proxy_allowing_destructive):
    present = _names(proxy_allowing_destructive._build_catalog_tools()[0]) & _DESTRUCTIVE
    assert present == _DESTRUCTIVE, f"Missing destructive tools when enabled: {_DESTRUCTIVE - present}"


def test_full_catalog_count(proxy_allowing_destructive):
    """With destructive enabled the proxy exposes every MCP tool (CLI-only commands
    are never MCP tools). Derived from mcp_tool_catalog.MCP_TOOLS, not a literal:
    a hand-edited count drifts (63 -> 110)."""
    tools = importlib.import_module("mcp_tool_catalog").MCP_TOOLS
    assert len(proxy_allowing_destructive._build_catalog_tools()[0]) == len(tools)


def test_catalog_schemas_is_the_one_builder(proxy):
    """The proxy's cached build and the uncached builder agree, so
    measure_tool_tokens (which uses the uncached one) measures what is served."""
    assert proxy._build_catalog_tools()[0] == proxy.catalog_schemas(include_destructive=False)[0]
    assert _names(proxy.catalog_schemas(include_destructive=True)[0]) >= _DESTRUCTIVE
