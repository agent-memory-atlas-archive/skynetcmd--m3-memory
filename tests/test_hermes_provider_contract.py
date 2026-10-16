"""The Hermes Agent memory-provider contract, checked without Hermes installed.

Hermes (>= 0.22) loads m3 through the ``hermes_agent.memory_providers`` entry
point and drives it through ``agent.memory_provider.MemoryProvider``. The live
proof is an end-to-end run inside a Hermes checkout; this pins the parts CI can
hold: the entry point and catalog manifest exist, the provider's surface, the
recall-only contexts, and the turn write's arguments (the provider once called
an old ``chatlog_write`` shape and no turn was ever captured).
Hermes' modules are stubbed; the m3 client is a recorder, so no store is touched.
"""

from __future__ import annotations

import importlib
import json
import sys
import tomllib
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HERMES_DIR = REPO / "m3_memory" / "integrations" / "hermes"


class _MemoryProvider:
    """The slice of Hermes' ABC the provider subclasses."""


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")
    mp.MemoryProvider = _MemoryProvider
    tools = types.ModuleType("tools")
    registry = types.ModuleType("tools.registry")
    registry.tool_error = lambda msg: json.dumps({"error": msg})
    consts = types.ModuleType("hermes_constants")
    consts.get_hermes_home = lambda: tmp_path  # a Path, as in Hermes
    for name, mod in {
        "agent": agent,
        "agent.memory_provider": mp,
        "tools": tools,
        "tools.registry": registry,
        "hermes_constants": consts,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "m3_memory.integrations.hermes", raising=False)
    return importlib.import_module("m3_memory.integrations.hermes")


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def _tool(self, name, **kw):
        self.calls.append((name, kw))
        return []

    def write_turns(self, turns, **kw):
        self.calls.append(("write_turns", {"turns": turns, **kw}))
        return len(turns)


def _provider(hermes, **init):
    p = hermes.M3MemoryProvider()
    p.initialize("s1", user_id="alice", **init)
    p._client = _Recorder()
    return p


def test_entry_point_is_declared():
    eps = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["entry-points"]
    assert eps["hermes_agent.memory_providers"]["m3"] == "m3_memory.integrations.hermes"


def test_catalog_manifest_matches_the_contract():
    text = (HERMES_DIR / "folder_plugin" / "plugin.yaml").read_text(encoding="utf-8")
    for line in ("name: m3", "kind: exclusive", "provides_tools: []"):
        assert line in text, line
    stub = (HERMES_DIR / "folder_plugin" / "__init__.py").read_text(encoding="utf-8")
    assert "from m3_memory.integrations.hermes import M3MemoryProvider, register" in stub


def test_host_agent_is_registered(hermes):
    import chatlog_config

    assert hermes._HOST_AGENT in chatlog_config.VALID_HOST_AGENTS


def test_surface(hermes):
    p = hermes.M3MemoryProvider()
    assert isinstance(p, _MemoryProvider) and p.name == "m3"
    assert [s["name"] for s in p.get_tool_schemas()] == ["m3_profile", "m3_search", "m3_conclude"]
    registered = []
    hermes.register(types.SimpleNamespace(register_memory_provider=registered.append))
    assert len(registered) == 1 and isinstance(registered[0], hermes.M3MemoryProvider)


def test_unavailable_reason_is_empty_exactly_when_available(hermes):
    p = hermes.M3MemoryProvider()
    assert p.is_available() == (p.unavailable_reason() == "")


def test_sync_turn_writes_through_the_shared_writer(hermes):
    p = _provider(hermes)
    p.sync_turn("hi", "hello", session_id="conv-9")
    p._sync_thread.join(timeout=10)
    assert p._client.calls == [
        (
            "write_turns",
            {
                "turns": [("user", "hi"), ("assistant", "hello")],
                "conversation_id": "conv-9",
                "host_agent": "hermes",
                "user_id": "alice",
            },
        )
    ]


@pytest.mark.parametrize("context", ["cron", "subagent"])
def test_non_primary_contexts_are_recall_only(hermes, context):
    p = _provider(hermes, agent_context=context)
    assert "recall-only" in p.handle_tool_call("m3_conclude", {"conclusion": "x"})
    p.sync_turn("a", "b")
    assert p._sync_thread is None
    p.handle_tool_call("m3_search", {"query": "q"})
    assert [c[0] for c in p._client.calls] == ["memory_search_scored"]


def test_tool_errors_go_through_hermes_tool_error(hermes):
    p = _provider(hermes)
    assert json.loads(p.handle_tool_call("nope", {})) == {"error": "Unknown tool: nope"}
    assert "Missing required parameter: query" in p.handle_tool_call("m3_search", {})


def test_conclude_and_recall_share_one_scope(hermes):
    p = _provider(hermes)
    p.handle_tool_call("m3_conclude", {"conclusion": "likes tea"})
    p.handle_tool_call("m3_search", {"query": "tea"})
    p.handle_tool_call("m3_profile", {})
    write, search, profile = (kw for _n, kw in p._client.calls)
    for kw in (write, search, profile):
        assert (kw["user_id"], kw["scope"]) == ("alice", "user")
    assert write["type"] == profile["type_filter"] == "user_fact"
    assert profile["query"] == ""  # the documented filter-only listing


def test_status_config_carries_the_unavailable_reason(hermes, monkeypatch, caplog):
    """Hermes prints only "not available"; the cause reaches the user through
    get_status_config (shown by `hermes memory status`) and one warning."""
    p = hermes.M3MemoryProvider()
    assert "unavailable" not in p.get_status_config({"user_id": "a"}) or not p.is_available()
    monkeypatch.setattr(hermes.M3MemoryProvider, "_unavailable_cause", staticmethod(lambda: "broken. fix: x"))
    monkeypatch.setattr(hermes, "_warned_unavailable", set())
    assert p.get_status_config({"user_id": "a"}) == {"user_id": "a", "unavailable": "broken. fix: x"}
    with caplog.at_level("WARNING"):
        assert not p.is_available() and not p.is_available()
    assert [r.getMessage() for r in caplog.records].count("m3 memory provider unavailable: broken. fix: x") == 1


def test_save_config_refuses_to_replace_an_unreadable_file(hermes, tmp_path):
    p = hermes.M3MemoryProvider()
    cfg = tmp_path / "m3.json"
    cfg.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not overwriting"):
        p.save_config({"user_id": "z"}, tmp_path)
    assert cfg.read_text(encoding="utf-8") == "{not json"
    cfg.write_text('{"agent_id": "kept"}', encoding="utf-8")
    p.save_config({"user_id": "z"}, tmp_path)
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"agent_id": "kept", "user_id": "z"}
