"""Semantic search ranks every in-scope memory, not a fixed 50.

The semantic candidate SQL took 50 items by the scoping predicates, in no order,
and ranked only those -- the same 50 rows for every query. On a live store of
thousands it returned 0 of the 40 true nearest memories over 5 queries
(measured 2026-10-15). Semantic search is also where every hybrid search lands
when its keywords match nothing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from embedding_utils import pack as _pack  # noqa: E402


def _vec(primary, dim):
    v = [0.0] * dim
    v[0] = primary
    v[1] = (1.0 - primary * primary) ** 0.5
    return v


@pytest.mark.asyncio
@pytest.mark.parametrize("rows_before_target", [60, 299])
async def test_the_nearest_memory_is_found_wherever_it_sits(monkeypatch, tmp_path, rows_before_target):
    import memory_core
    from memory.config import EMBED_DIM, EMBED_MODEL

    from conftest import create_full_main_schema

    db = tmp_path / "t.db"
    create_full_main_schema(db)
    with sqlite3.connect(str(db)) as conn:
        for i in range(300):
            mid = "target" if i == rows_before_target else f"other-{i}"
            primary = 1.0 if mid == "target" else 0.2
            conn.execute(
                "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
                "created_at, importance, is_deleted) VALUES (?,?,?,?,?,?,?,?,0)",
                (mid, "note", mid, f"text {i}", "agent", "claude", f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z", 0.5),
            )
            conn.execute(
                "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) VALUES (?,?,?,?)",
                (mid, _pack(_vec(primary, EMBED_DIM)), EMBED_MODEL, EMBED_DIM),
            )

    @contextmanager
    def fake_db(existing=None, *a, **k):
        if existing is not None:
            yield existing
            return
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def fake_embed(_q):
        return (_vec(1.0, EMBED_DIM), "test-embed")

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_core, "_embed", fake_embed)
    res = await memory_core.memory_search_scored_impl(query="q", k=3, mmr=False, search_mode="semantic")
    assert res and res[0][1]["id"] == "target", [r[1]["id"] for r in res]


def test_semantic_sql_ranks_the_whole_scope_when_sqlite_vec_is_absent():
    """The no-vec SQL returns every in-scope row: ranking happens in the caller."""
    from memory.search_routing import build_candidate_sql

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memory_items (id TEXT PRIMARY KEY, content TEXT, title TEXT, type TEXT,
                                   importance REAL, is_deleted INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE memory_embeddings (memory_id TEXT, embedding BLOB, vector_kind TEXT);
        """
    )
    for i in range(120):
        conn.execute("INSERT INTO memory_items VALUES (?, '', '', 'note', 0.5, 0, '')", (f"m{i}",))
        conn.execute(
            "INSERT INTO memory_embeddings VALUES (?, x'00', ?)", (f"m{i}", "default" if i % 2 else "window_0")
        )
    sql = build_candidate_sql(
        search_mode="semantic",
        has_vec=False,
        where_sql="mi.is_deleted = 0 AND me.vector_kind = 'default'",
        row_limit=2000,
    )
    ids = {r[0] for r in conn.execute(sql)}
    assert len(ids) == 60, "every default-kind row, and only those"
