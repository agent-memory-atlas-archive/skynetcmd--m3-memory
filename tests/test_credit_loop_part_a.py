"""Memory credit loop part A: grading is reachable and
measured. Agents are told to grade (through m3_call; the startup set is
unchanged), memory_search hands back the ids to grade, and the *_slim search
omits that handle."""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest


def test_the_server_instructions_tell_agents_to_grade():
    import memory_bridge

    text = memory_bridge._SERVER_INSTRUCTIONS
    assert "GRADE WHAT YOU USED" in text
    assert 'm3_call("memory_grade"' in text and "grade_with" in text


def test_memory_grade_is_reachable_without_joining_the_startup_set():
    import mcp_tool_catalog
    import tool_domains

    assert "memory_grade" not in tool_domains.ESSENTIAL_TOOL_NAMES  # Q1: zero added tokens
    assert "m3_call" in tool_domains.ESSENTIAL_TOOL_NAMES
    spec = {t.name: t for t in mcp_tool_catalog.TOOLS}["memory_grade"]
    assert spec.mcp and spec.default_allowed


def test_the_slim_search_fixes_the_handle_off_and_validate_args_applies_it():
    import mcp_tool_catalog
    from catalog.dispatch import validate_args

    specs = {t.name: t for t in mcp_tool_catalog.TOOLS}
    slim, full = specs["memory_search_slim"], specs["memory_search"]
    assert slim.impl is full.impl
    assert dict(slim.fixed_args) == {"grade_with": False}
    assert not full.fixed_args
    args, err = validate_args(slim, {"query": "q", "grade_with": True})
    assert err is None and args["grade_with"] is False, "a fixed argument overrides the caller's"


@pytest.fixture
def ranked(monkeypatch):
    search = importlib.import_module("memory.search")
    rows = [
        (0.9, {"id": "id-a", "type": "note", "title": "a", "content": "x"}),
        (0.5, {"id": "id-b", "type": "note", "title": "b", "content": "y"}),
    ]

    async def _scored(*a, **k):
        return rows

    monkeypatch.setattr(search, "memory_search_scored_impl", _scored)
    monkeypatch.setattr(search, "_resolve_mc_callbacks", lambda: None)
    return search


def test_search_lists_the_ids_to_grade(ranked):
    out = asyncio.run(ranked.memory_search_impl("q"))
    assert 'grade_with: ["id-a", "id-b"]' in out
    rec = json.loads(asyncio.run(ranked.memory_search_impl("q", as_records=True)))
    assert rec["grade_with"] == ["id-a", "id-b"]


def test_search_without_the_handle_omits_it(ranked):
    assert "grade_with" not in asyncio.run(ranked.memory_search_impl("q", grade_with=False))
    rec = json.loads(asyncio.run(ranked.memory_search_impl("q", as_records=True, grade_with=False)))
    assert "grade_with" not in rec


def test_on_conflict_add_adds_on_each_backend(each_backend):
    """The dialect primitive behind the daily record: a second insert ADDS."""
    from memory.backends import dialect

    d = dialect()
    p = d.param()
    with each_backend.connection() as c:
        cur = c.cursor()
        cur.execute("CREATE TEMP TABLE t_add (k TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0)")
        for _ in range(3):
            cur.execute(
                f"INSERT INTO t_add (k, n) VALUES ({p}, {p}) " + d.on_conflict_add("(k)", "t_add", ["n"]), ("x", 2)
            )
        cur.execute("SELECT n FROM t_add")
        assert cur.fetchone()[0] == 6
