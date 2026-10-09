"""Search modes: what the schemas offer is what the search implements.

The memory search schemas offered "keyword" while memory_search_scored_impl
understood only "fts5", so a client choosing keyword search, as the schema
offered, always got an empty result; an unknown mode was equally silent.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest
from m3_core.search_modes import PUBLIC_SEARCH_MODES, normalize_search_mode


@pytest.fixture
def lexical_row():
    """A lexical-only row (embed=False) with a word no other row has, deleted
    afterwards: on PostgreSQL the store is a shared database that outlives the test."""
    import re
    import uuid

    mc = importlib.import_module("memory_core")
    word = f"zq{uuid.uuid4().hex[:12]}"
    out = asyncio.run(mc.memory_write_impl(type="note", content=f"the server listens on {word}",
                                           title=f"server {word}", embed=False))
    mid = re.search(r"[0-9a-f-]{36}", str(out)).group(0)
    yield word, mid
    mc.memory_delete_impl(mid, hard=True)


def test_keyword_search_finds_a_lexical_match_on_each_backend(each_backend, lexical_row):
    mc = importlib.import_module("memory_core")
    word, mid = lexical_row
    hits = asyncio.run(mc.memory_search_scored_impl(word, k=5, search_mode="keyword"))
    assert [h[1]["id"] for h in hits] == [mid], f"keyword search on {each_backend.name}: {hits}"


@pytest.mark.parametrize("bad", ["nonsense", "vector", ""])
def test_an_unknown_mode_is_an_error_not_an_empty_result(bad):
    mc = importlib.import_module("memory_core")
    with pytest.raises(ValueError, match="hybrid, semantic, keyword"):
        asyncio.run(mc.memory_search_scored_impl("server port", k=5, search_mode=bad))


def test_every_public_mode_resolves_and_fts5_stays_internal():
    assert [normalize_search_mode(m) for m in PUBLIC_SEARCH_MODES] == ["hybrid", "semantic", "fts5"]
    assert normalize_search_mode(" Keyword ") == "fts5"
    assert normalize_search_mode("fts5") == "fts5"  # internal callers
    assert "fts5" not in PUBLIC_SEARCH_MODES


# ── chat log search: lexical only; it advertised "vector" and ignored the value ──
@pytest.mark.parametrize("legacy", ["keyword", "hybrid", "fts", "fts5", "", " Keyword "])
def test_chatlog_accepts_every_name_it_honours(legacy):
    from m3_core.search_modes import normalize_chatlog_search_mode

    assert normalize_chatlog_search_mode(legacy) == "keyword"


@pytest.mark.parametrize("bad", ["vector", "semantic", "nonsense"])
def test_chatlog_refuses_a_mode_it_cannot_run(bad):
    """Refused at the tool's entry, before any flush or query, instead of
    silently running keyword search under a vector label."""
    import chatlog_core

    with pytest.raises(ValueError, match=r"lexical .FTS5. only"):
        asyncio.run(chatlog_core.chatlog_search_impl("anything", search_mode=bad))


def test_chatlog_search_schema_offers_only_what_it_runs():
    from m3_core.search_modes import CHATLOG_SEARCH_MODES

    cat = importlib.import_module("mcp_tool_catalog")
    prop = cat._BY_NAME["chatlog_search"].parameters["properties"]["search_mode"]
    assert prop["enum"] == list(CHATLOG_SEARCH_MODES) and prop["default"] in prop["enum"]


def test_every_tool_schema_offers_exactly_the_public_modes():
    cat = importlib.import_module("mcp_tool_catalog")
    offering = {t.name: t.parameters["properties"]["search_mode"].get("enum")
                for t in cat.TOOLS if "search_mode" in t.parameters.get("properties", {})
                and not t.name.startswith("chatlog_")}
    assert offering, "no tool takes search_mode"
    wrong = {name: enum for name, enum in offering.items() if enum != list(PUBLIC_SEARCH_MODES)}
    assert not wrong, f"schema enum differs from m3_core.search_modes: {wrong}"
