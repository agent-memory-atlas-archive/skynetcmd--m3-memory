"""memory.config serves registry-backed names at CALL time (plan 2D.1).

Before 2D.1 they were env reads bound at import: m3_config.toml never reached
them, and neither did the settings test fixture, so a test that wanted a value
had to patch the module attribute and depended on import order.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest


def _cfg():
    return importlib.import_module("memory.config")


def test_every_served_name_is_a_registry_key_and_unbound():
    cfg = _cfg()
    assert len(cfg.SETTING_NAMES) > 60
    bound = sorted(n for n in cfg.SETTING_NAMES if n in vars(cfg))
    assert not bound, f"bound at import, so not read at call time: {bound}"
    for name in cfg.SETTING_NAMES:
        getattr(cfg, name)  # every accessor resolves (a bad key name raises)


def test_the_settings_fixture_reaches_config_and_the_shim(m3_settings):
    """The old constants and memory_core's caching shim both fail this."""
    cfg = _cfg()
    mc = importlib.import_module("memory_core")
    m3_settings({"search.row_cap": 123, "embed.model": "m-1"})
    assert cfg.SEARCH_ROW_CAP == 123 and mc.SEARCH_ROW_CAP == 123
    assert cfg.EMBED_SPACE_TAG == "m-1", "a derived name follows the key it falls back to"
    m3_settings({"search.row_cap": 456})
    assert mc.SEARCH_ROW_CAP == 456, "memory_core cached the first value"


def test_a_monkeypatched_name_is_patched_now(monkeypatch):
    """Paired with the next test: monkeypatch's undo re-binds the value it read
    as a REAL attribute; conftest must drop it so the accessor serves again."""
    cfg = _cfg()
    monkeypatch.setattr(cfg, "SEARCH_ROW_CAP", 7)
    assert cfg.SEARCH_ROW_CAP == 7


def test_no_patched_name_outlives_its_test():
    assert "SEARCH_ROW_CAP" not in vars(_cfg()), "a stale binding shadows the accessor"


def test_dedup_reads_the_configured_threshold(m3_settings, monkeypatch):
    mm = importlib.import_module("memory_maintenance")
    import m3_sdk

    monkeypatch.setattr(m3_sdk, "_LAST_USER_INTERACTION", 0.0)
    m3_settings({"memory.dedup.threshold": 0.5})
    assert asyncio.run(_maybe_await(mm.memory_dedup_impl(dry_run=True)))["threshold"] == 0.5


def test_the_dedup_tool_advertises_no_copied_default():
    """The MCP bridge passes a schema default explicitly, which would override
    the configured threshold."""
    cat = importlib.import_module("mcp_tool_catalog")
    assert "default" not in cat._BY_NAME["memory_dedup"].parameters["properties"]["threshold"]


# An expansion row keeps rank 1 only if its score >= margin * the primary's:
# 1.0 >= 1.05 * 0.9 holds, 1.0 >= 1.5 * 0.9 does not.
@pytest.mark.parametrize("margin,expect_swap", [(1.5, True), (1.05, False)])
def test_the_expansion_guard_reads_its_settings_at_call_time(m3_settings, margin, expect_swap):
    sr = importlib.import_module("memory.search_routing")
    m3_settings({"search.expansion_displacement_margin": margin, "search.expansion_protected_ranks": 1})
    hits = [(1.0, {"id": "x", "_expanded_via": "entity"}), (0.9, {"id": "p"})]
    out = sr._enforce_expansion_displacement_guard(list(hits))
    assert (out[0][1]["id"] == "p") is expect_swap


def test_doctor_still_renames_every_registry_alias():
    """The aliases moved off DEPRECATED_ENV_RENAMES (getenv_compat's map) must
    still reach `m3 doctor`'s on-disk rename, through the registry."""
    from m3_core.config import REGISTRY
    from m3_core.paths import DEPRECATED_ENV_RENAMES, all_env_renames

    renames = all_env_renames()
    for key in REGISTRY:
        for alias in key.env_names[1:]:
            assert renames.get(alias) == key.env_names[0], (alias, key.name)
    assert renames["TITLE_MATCH_BOOST"] == "M3_TITLE_MATCH_BOOST"
    # An alias is in both while a reader still calls getenv_compat (2D.2 converts
    # those; the drift test then forces the map entry out). Meanwhile the two
    # must name the same canonical variable.
    registry = {a: k.env_names[0] for k in REGISTRY for a in k.env_names[1:]}
    disagree = {
        a: (DEPRECATED_ENV_RENAMES[a], registry[a])
        for a in set(registry) & set(DEPRECATED_ENV_RENAMES)
        if DEPRECATED_ENV_RENAMES[a] != registry[a]
    }
    assert not disagree, f"getenv_compat map vs registry: {disagree}"


async def _maybe_await(v):
    return await v if asyncio.iscoroutine(v) else v
