"""The resident vector index picks semantic candidates without reading every blob.

Exercised with a stand-in for the Rust core's VectorIndex (exact cosine, same
interface), so the m3 side -- freshness, scope, the keyed SQL -- is tested on
every lane whether or not the installed core has the index.
"""

from __future__ import annotations

import asyncio
import importlib
import math
import os
import sqlite3
import struct
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from embedding_utils import pack as _pack  # noqa: E402


class _FakeIndex:
    loads: list[int] = []

    def __init__(self, dim):
        self.dim, self.vecs = dim, {}

    def upsert_packed(self, keys, blobs):
        row = self.dim * 4
        for i, k in enumerate(keys):
            self.vecs[k] = struct.unpack(f"{self.dim}f", blobs[i * row : (i + 1) * row])
        _FakeIndex.loads.append(len(keys))

    def search(self, q, k, allowed=None):
        def cos(v):
            n = math.sqrt(sum(x * x for x in v)) * math.sqrt(sum(x * x for x in q))
            return sum(a * b for a, b in zip(v, q, strict=False)) / n if n else 0.0

        keys = [a for a in (allowed if allowed is not None else self.vecs) if a in self.vecs]
        return sorted(((a, cos(self.vecs[a])) for a in keys), key=lambda t: -t[1])[:k]

    def __len__(self):
        return len(self.vecs)


class _FakeCore:
    VectorIndex = _FakeIndex


def _vec(primary, dim):
    v = [0.0] * dim
    v[0], v[1] = primary, (1.0 - primary * primary) ** 0.5
    return v


@pytest.fixture
def store(monkeypatch, tmp_path):
    import memory_core
    from memory.config import EMBED_DIM, EMBED_MODEL

    from conftest import create_full_main_schema

    cfg = importlib.import_module("memory.config")
    vi = importlib.import_module("memory.vector_index")
    monkeypatch.setattr(cfg, "m3_core_rs", _FakeCore())
    vi.reset()
    _FakeIndex.loads = []
    db = tmp_path / "t.db"
    create_full_main_schema(db)
    with sqlite3.connect(str(db)) as conn:
        for i in range(120):
            conn.execute(
                "INSERT INTO memory_items (id, type, title, content, source, change_agent, created_at, importance,"
                " is_deleted) VALUES (?, 'note', ?, ?, 'agent', 'claude', '2026-01-01T00:00:00Z', 0.5, 0)",
                (f"m{i}", f"m{i}", f"text {i}"),
            )
            conn.execute(
                "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim, content_hash) VALUES (?,?,?,?,?)",
                (f"m{i}", _pack(_vec(1.0 if i == 77 else 0.2, EMBED_DIM)), EMBED_MODEL, EMBED_DIM, f"h{i}"),
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
    yield memory_core, db, EMBED_DIM
    vi.reset()


def _top(mc):
    res = asyncio.run(mc.memory_search_scored_impl(query="q", k=3, mmr=False, search_mode="semantic"))
    return [r[1]["id"] for r in res]


def test_the_index_finds_the_nearest_and_loads_each_row_once(store):
    mc, _db, _dim = store
    assert _top(mc)[0] == "m77"
    assert sum(_FakeIndex.loads) == 120
    assert _top(mc)[0] == "m77"
    assert sum(_FakeIndex.loads) == 120, "a second search must not reload unchanged rows"


def test_an_embedding_rewritten_in_place_is_reloaded(store):
    """pg_sync's pull updates an embedding under the same rowid."""
    mc, db, dim = store
    assert _top(mc)[0] == "m77"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE memory_embeddings SET embedding = ?, content_hash = 'new' WHERE memory_id = 'm77'",
            (_pack(_vec(0.1, dim)),),
        )
        conn.execute(
            "UPDATE memory_embeddings SET embedding = ?, content_hash = 'new' WHERE memory_id = 'm5'",
            (_pack(_vec(1.0, dim)),),
        )
    assert _top(mc)[0] == "m5"


def test_a_row_out_of_scope_never_comes_back(store):
    mc, db, _dim = store
    assert _top(mc)[0] == "m77"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE memory_items SET is_deleted = 1 WHERE id = 'm77'")
    assert "m77" not in _top(mc), "a deleted memory is still in the index but must not be returned"


def test_without_the_index_the_full_scan_still_answers(store, monkeypatch):
    mc, _db, _dim = store

    class _OldCore:
        pass

    monkeypatch.setattr(importlib.import_module("memory.config"), "m3_core_rs", _OldCore())
    assert _top(mc)[0] == "m77"
    assert not _FakeIndex.loads


def test_nearest_itself_sees_an_in_place_rewrite(store):
    """With k below the store size, a stale vector would pick the wrong row; the
    search-level test above cannot show that, since exact re-scoring of a pool
    larger than this store hides it."""
    _mc, db, dim = store
    vi = importlib.import_module("memory.vector_index")
    conn = sqlite3.connect(str(db))
    where, params = "mi.is_deleted = 0", ()

    def best():
        rid = vi.nearest(conn, dim=dim, where_sql=where, params=params, q_vec=_vec(1.0, dim), k=1)[0]
        return conn.execute("SELECT memory_id FROM memory_embeddings WHERE rowid = ?", (rid,)).fetchone()[0]

    assert best() == "m77"
    conn.execute(
        "UPDATE memory_embeddings SET embedding = ?, content_hash = 'new' WHERE memory_id = 'm77'",
        (_pack(_vec(0.1, dim)),),
    )
    conn.execute(
        "UPDATE memory_embeddings SET embedding = ?, content_hash = 'new' WHERE memory_id = 'm5'",
        (_pack(_vec(1.0, dim)),),
    )
    conn.commit()
    assert best() == "m5"
    conn.close()


def test_ties_are_broken_by_id_whatever_the_arrival_order():
    """Duplicate memories share a vector; the scan and the index deliver rows in
    different orders. The cut must pick the same copies either way."""
    from memory.search import _best_by_score

    ids, scores = ["zz", "aa", "mm", "bb"], [0.5, 0.5, 0.9, 0.1]
    forward = [ids[i] for i in _best_by_score(ids, scores, 2)]
    rev_ids, rev_scores = ids[::-1], scores[::-1]
    backward = [rev_ids[i] for i in _best_by_score(rev_ids, rev_scores, 2)]
    assert forward == backward == ["mm", "aa"]


def test_a_connection_the_index_cannot_use_falls_back_to_the_scan(store, caplog):
    """A mock or foreign connection must cost the index, not the search."""
    vi = importlib.import_module("memory.vector_index")

    class _Mock:
        def execute(self, *a, **k):
            raise AttributeError("MockCursor has no fetchone")

    assert vi.nearest(_Mock(), dim=4, where_sql="1=1", params=(), q_vec=[1.0, 0, 0, 0], k=3) is None
    assert "scanning in full" in caplog.text
