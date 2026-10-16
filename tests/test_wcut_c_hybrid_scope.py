"""Hybrid search applies the caller's scope BEFORE the FTS row cap.

The hybrid candidate SQL ranked the GLOBAL FTS match set (every user, agent and
deleted row) and cut it at the row cap before the scope predicates ran. On a
shared store other users' stronger matches filled the cap, and in-scope matches
were silently dropped -- not ranked low, absent.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from embedding_utils import pack as _pack  # noqa: E402

OTHER_ROWS = 2100  # above the hybrid SQL row cap (2000)


def _vec(primary, dim):
    v = [0.0] * dim
    v[0] = primary
    v[1] = (1.0 - primary * primary) ** 0.5
    return v


@pytest.mark.asyncio
async def test_in_scope_match_survives_other_users_stronger_matches(monkeypatch, tmp_path):
    import memory_core
    from memory.config import EMBED_DIM, EMBED_MODEL

    from conftest import create_full_main_schema

    db = tmp_path / "t.db"
    create_full_main_schema(db)
    blob = _pack(_vec(0.5, EMBED_DIM))
    near = _pack(_vec(1.0, EMBED_DIM))
    with sqlite3.connect(str(db)) as conn:
        rows = [(f"other-{i}", "otheruser", "zorblax zorblax zorblax zorblax note") for i in range(OTHER_ROWS)]
        # The in-scope rows match WEAKLY (one occurrence in a long text), so a
        # global bm25 ranking puts every one of them below the other user's rows.
        rows += [
            (f"mine-{i}", "me", f"zorblax variant{i} " + " ".join(f"filler{j}" for j in range(60))) for i in range(3)
        ]
        # In-scope decoys WITHOUT the keyword but nearer the query vector: a
        # semantic pass (the fallback when the hybrid fetch comes back empty)
        # ranks these first, so it cannot mask a dropped keyword match.
        rows += [(f"decoy-{i}", "me", f"unrelated text number {i}") for i in range(20)]
        for mid, uid, content in rows:
            conn.execute(
                "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
                "created_at, importance, is_deleted, user_id) VALUES (?,?,?,?,?,?,?,?,0,?)",
                (mid, "note", "t", content, "agent", "claude", "2026-01-01T00:00:00Z", 0.5, uid),
            )
            conn.execute(
                "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) VALUES (?,?,?,?)",
                (mid, near if mid.startswith("decoy") else blob, EMBED_MODEL, EMBED_DIM),
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

    res = await memory_core.memory_search_scored_impl(
        query="zorblax", k=3, mmr=False, search_mode="hybrid", user_id="me"
    )
    ids = sorted(it["id"] for _, it in res)
    assert ids == ["mine-0", "mine-1", "mine-2"], ids
