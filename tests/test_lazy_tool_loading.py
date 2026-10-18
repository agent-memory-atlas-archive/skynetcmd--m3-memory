"""Regression test for the lazy domain-loading MCP tool catalog.

Covers the code paths introduced in commit 0cfbb9f (feat(mcp): lazy
domain-loading cuts startup tool catalog 85%) and 73ce2d1's surrounding
plumbing:

  bin/tool_domains.py     — name → domain mapping, essentials allowlist
  bin/tool_loader.py      — meta-tool impls + bridge-callback registry
  bin/mcp_tool_catalog.py — `tools_list_domains`, `tools_load_domain` meta-tools
  bin/memory_bridge.py    — gated registration + the register-domain callback

Goal: catch regressions that would silently re-enable the legacy eager-mode
behavior, drop the meta-tools, or leave catalog tools un-bucketed in any
domain.

This test does NOT spin up a FastMCP server — it works against the catalog
and the helper modules directly. The bridge-side integration is covered by
a separate test (`test_lazy_tool_loading_bridge_integration`) that imports
memory_bridge under controlled env vars.
"""

from __future__ import annotations

import json
import os
import sys

# conftest.py already puts bin/ on sys.path. Belt-and-suspenders so this
# file is also importable in isolation:
_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import mcp_tool_catalog
import tool_domains
import tool_loader

# ── tool_domains.py ──────────────────────────────────────────────────────────


def test_every_catalog_tool_maps_to_a_known_domain():
    """No tool should fall through to the 'admin' catch-all by accident.

    Domain assignment is name-prefix based. Adding a new tool with a name
    that doesn't match any prefix would silently bucket it under 'admin',
    where the agent would never load it on demand for the intuitive
    domain. This test forces the new-tool author to extend
    `_DOMAIN_PREFIXES` explicitly.
    """
    # Build the set of "intentional admin tools" — name prefixes explicitly
    # routed to admin in _DOMAIN_PREFIXES.
    intentional_admin_prefixes = {p for p, d in tool_domains._DOMAIN_PREFIXES if d == "admin"}

    orphans = []
    for spec in mcp_tool_catalog.TOOLS:
        # Meta-tools (tools_*) intentionally land in admin via the prefix
        # fallback — they're cross-cutting.
        if spec.name.startswith("tools_"):
            continue
        domain = tool_domains.domain_of_tool(spec.name)
        if domain != "admin":
            continue
        # If we got here it's an admin tool — confirm there's an explicit
        # prefix rule (not just the trailing fallback).
        if not any(spec.name == p or spec.name.startswith(p) for p in intentional_admin_prefixes):
            orphans.append(spec.name)

    assert not orphans, (
        f"Tool(s) fell through to the 'admin' catch-all without an explicit "
        f"prefix rule in tool_domains._DOMAIN_PREFIXES: {sorted(orphans)}. "
        f"Add a prefix entry to assign them to the right domain."
    )


def test_essentials_are_real_catalog_tools():
    """Every entry in ESSENTIAL_TOOL_NAMES must exist in the catalog.

    A typo in the essentials set would silently drop a tool from the
    always-on surface, making `m3 setup` startup look unhealthy.
    """
    catalog_names = {t.name for t in mcp_tool_catalog.MCP_TOOLS}  # an essential must be an MCP tool
    missing = tool_domains.ESSENTIAL_TOOL_NAMES - catalog_names
    assert not missing, f"Essentials set references tool(s) not in the catalog: {sorted(missing)}"


def test_essentials_include_search_for_each_searched_store():
    """The startup set searches the two stores agents actually search, memory
    and chat (files_search drew no direct calls and is one m3_call away; user
    decision 2026-10-17), and always carries m3_call, the route to the rest."""
    primary_search_tools = {"memory_search", "chatlog_search", "m3_call"}
    assert primary_search_tools <= tool_domains.ESSENTIAL_TOOL_NAMES, (
        f"Essentials missing primary-store search tools: "
        f"{sorted(primary_search_tools - tool_domains.ESSENTIAL_TOOL_NAMES)}"
    )


def test_domain_descriptions_cover_every_domain_seen_in_catalog():
    """DOMAIN_DESCRIPTIONS feeds the `tools_list_domains` response. Every
    domain that actually has tools must have a description, or the agent
    will see a domain in the list with no text."""
    seen = {tool_domains.domain_of_tool(t.name) for t in mcp_tool_catalog.TOOLS}
    described = set(tool_domains.DOMAIN_DESCRIPTIONS.keys())
    missing = seen - described
    assert not missing, f"Domain(s) have catalog tools but no description in DOMAIN_DESCRIPTIONS: {sorted(missing)}"


def test_group_by_domain_is_complete():
    """`group_by_domain` over every catalog name should account for every
    catalog tool — no silent drops."""
    names = [t.name for t in mcp_tool_catalog.TOOLS]
    grouped = tool_domains.group_by_domain(names)
    flat = [n for ns in grouped.values() for n in ns]
    assert sorted(flat) == sorted(names), (
        "group_by_domain dropped or duplicated tools — set diff: "
        f"missing={sorted(set(names) - set(flat))}, "
        f"extra={sorted(set(flat) - set(names))}"
    )


# ── tool_loader.py meta-tool impls ───────────────────────────────────────────


def test_list_domains_returns_well_formed_json():
    """`tools_list_domains` impl returns a JSON string with a `domains`
    array, each entry having name + description + tool_count."""
    raw = tool_loader.list_domains()
    data = json.loads(raw)
    assert "domains" in data, f"missing 'domains' key in {data}"
    for entry in data["domains"]:
        assert "name" in entry
        assert "description" in entry
        assert "tool_count" in entry
        assert isinstance(entry["tool_count"], int)
        assert entry["tool_count"] >= 0
    # At least one domain must have tools — otherwise something pruned the catalog.
    assert any(e["tool_count"] > 0 for e in data["domains"])


def test_load_domain_with_unknown_name_returns_error():
    """Calling `tools_load_domain(domain='nope')` shouldn't crash; it
    should return a usable error message the agent can read."""
    out = tool_loader.load_domain("nope")
    assert "Error" in out or "unknown" in out.lower(), out


def test_load_domain_without_bridge_callback_returns_explainer():
    """If the bridge hasn't bound a callback (e.g. running outside the
    MCP server), `tools_load_domain` should explain that rather than
    silently failing."""
    # Save + clear the callback, then restore.
    saved = tool_loader._register_domain_callback
    tool_loader._register_domain_callback = None
    try:
        raw = tool_loader.load_domain("memory")
        data = json.loads(raw)
        assert data.get("status") == "callback-not-bound", data
    finally:
        tool_loader._register_domain_callback = saved


def test_load_domain_with_bound_callback_invokes_it():
    """Bind a stub callback, call `tools_load_domain`, confirm the
    callback was hit and the result is JSON-serialized."""
    seen = []

    def stub(domain):
        seen.append(domain)
        return {"domain": domain, "tools_now_available": ["x", "y"], "tools_total_registered": 8, "schemas": []}

    saved = tool_loader._register_domain_callback
    tool_loader.set_register_domain_callback(stub)
    try:
        raw = tool_loader.load_domain("files")
        data = json.loads(raw)
        assert seen == ["files"], seen
        assert data["tools_now_available"] == ["x", "y"]
        assert data["tools_total_registered"] == 8
    finally:
        tool_loader._register_domain_callback = saved


# ── mcp_tool_catalog.py — meta-tool entries ──────────────────────────────────


def test_meta_tools_registered_in_catalog():
    """Both `tools_list_domains` and `tools_load_domain` must appear in
    `mcp_tool_catalog.TOOLS` — without them, lazy mode has no escape hatch."""
    names = {t.name for t in mcp_tool_catalog.TOOLS}
    assert "tools_list_domains" in names
    assert "tools_load_domain" in names


def test_meta_tools_are_default_allowed():
    """If meta-tools end up `default_allowed=False` they'd be hidden by
    the destructive-tool gate and lazy mode would break."""
    by_name = {t.name: t for t in mcp_tool_catalog.TOOLS}
    for n in ("tools_list_domains", "tools_load_domain"):
        assert by_name[n].default_allowed, f"{n} must be default_allowed=True"


def test_load_domain_spec_requires_domain_arg():
    """The `tools_load_domain` schema must list `domain` as required —
    otherwise the agent might call it with no arg and the impl would
    raise a `KeyError`."""
    by_name = {t.name: t for t in mcp_tool_catalog.TOOLS}
    params = by_name["tools_load_domain"].parameters
    assert "domain" in params.get("required", []), f"tools_load_domain.parameters.required missing 'domain': {params}"


# ── Integration with memory_bridge ───────────────────────────────────────────


def _reload_bridge(m3_settings, values: dict) -> object:
    """Import / re-import memory_bridge with the given settings (m3 reads them
    from m3_config.toml, never the environment). The bridge reads tools.lazy and
    tools.startup at import time, so the cached module is popped first and the
    next import builds a clean FastMCP instance."""
    m3_settings(values)
    for mod in ("memory_bridge", "tool_loader"):
        sys.modules.pop(mod, None)
    import memory_bridge

    return memory_bridge


def test_bridge_lazy_mode_registers_only_the_startup_set(m3_settings):
    """With tools.lazy on (the default) and no startup
    config, exactly the shipped default registers."""
    mb = _reload_bridge(m3_settings, {"tools.lazy": True})
    expected = set(tool_domains.ESSENTIAL_TOOL_NAMES)
    assert mb.STARTUP_TOOLS == expected
    assert mb._REGISTERED == expected, (
        f"lazy mode registered the wrong set: "
        f"missing={sorted(expected - mb._REGISTERED)}, "
        f"extra={sorted(mb._REGISTERED - expected)}"
    )


def test_bridge_eager_mode_registers_all_tools(m3_settings):
    """With tools.lazy off, every MCP tool is registered up-front (the legacy
    behavior); CLI-only commands never are."""
    mb = _reload_bridge(m3_settings, {"tools.lazy": False})
    assert mb.STARTUP_TOOLS is None
    all_names = {t.name for t in mcp_tool_catalog.MCP_TOOLS}
    assert mb._REGISTERED == all_names, f"eager mode missed: {sorted(all_names - mb._REGISTERED)}"


def test_bridge_lazy_mode_domain_callback_registers_domain(m3_settings):
    """Domain callback should add exactly that domain's tools, return
    schemas, and be idempotent on a second call."""
    mb = _reload_bridge(m3_settings, {"tools.lazy": True})
    baseline = set(mb._REGISTERED)

    # Pick a domain we know has tools and isn't already registered.
    target = "entity"
    assert target in tool_domains.DOMAIN_DESCRIPTIONS

    result = mb._register_domain_callback(target)
    assert result["domain"] == target
    expected_new = set(tool_domains.domain_tool_names([t.name for t in mcp_tool_catalog.MCP_TOOLS], target)) - baseline
    assert set(result["tools_now_available"]) == expected_new
    assert mb._REGISTERED == baseline | expected_new

    # Schemas list parallels tools_now_available.
    assert len(result["schemas"]) == len(result["tools_now_available"])
    for s in result["schemas"]:
        assert {"name", "description", "inputSchema"} <= set(s.keys())

    # Second call: idempotent — no new tools registered.
    second = mb._register_domain_callback(target)
    assert second["tools_now_available"] == []
    assert mb._REGISTERED == baseline | expected_new


def test_bridge_registers_a_configured_startup_set(m3_settings):
    """tools.startup reached only `m3 doctor`'s report; the bridge registered its
    own list (2026-10-17). It now registers what tools_config resolves."""
    mb = _reload_bridge(m3_settings, {"tools.lazy": True, "tools.startup": ["memory_get", "m3_call"]})
    assert mb._REGISTERED == {"memory_get", "m3_call"}


def test_bridge_falls_back_loudly_on_an_unusable_startup_set(caplog, m3_settings):
    """An unknown name must not stop the server (that drops every memory tool):
    the shipped default loads and the error is logged."""
    import logging

    with caplog.at_level(logging.ERROR):
        mb = _reload_bridge(m3_settings, {"tools.lazy": True, "tools.startup": ["memory_gett", "m3_call"]})
    assert mb._REGISTERED == set(tool_domains.ESSENTIAL_TOOL_NAMES)
    assert "memory_gett" in caplog.text
