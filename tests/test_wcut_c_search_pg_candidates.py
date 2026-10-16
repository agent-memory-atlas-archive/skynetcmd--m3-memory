"""PostgreSQL candidate fetch keeps vector-only candidates and one vector space.

fetch_candidates_pg put keyword hits first and cut the union at row_limit, so a
full keyword side dropped every vector-only candidate. Its hydrate step also
filtered embeddings by dim only, so rows from another embed_model or vector_kind
came back as extra candidates. No cluster needed: the seam calls are faked and
the hydrate SQL is captured.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


class _Cur:
    def __init__(self, store):
        self.store = store
        self.description = [
            (c,) for c in ("id", "content", "title", "type", "importance", "embedding", "metadata_json")
        ]
        self._rows = []

    def execute(self, sql, params):
        self.store["sql"], self.store["params"] = sql, params
        n_ids = len(self.store["requested"])
        ids = params[:n_ids]
        self._rows = [(mid, "c", "t", "note", 0.5, b"\x00", None) for mid in ids]

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _Cur(self.store)


def _backend(kw_ids, vec_ids, store):
    from memory.backends.dialect import dialect_for

    pg = dialect_for("postgres")

    def keyword_search(conn, query, *, limit, **kw):
        return [SimpleNamespace(memory_id=m, score=-1.0) for m in kw_ids[:limit]]

    def vector_search(conn, q, *, limit, **kw):
        return [SimpleNamespace(memory_id=m, score=0.9) for m in vec_ids[:limit]]

    return SimpleNamespace(keyword_search=keyword_search, vector_search=vector_search, dialect=lambda: pg)


def _fetch(kw_ids, vec_ids, *, row_limit, vector_kind="default"):
    from memory.search_pg import fetch_candidates_pg

    store: dict = {}
    store["requested"] = list(dict.fromkeys([*kw_ids[:row_limit], *vec_ids[:row_limit]]))
    rows, has_vec = fetch_candidates_pg(
        _backend(kw_ids, vec_ids, store),
        _Conn(store),
        query="q",
        q_vec=[0.1, 0.2],
        search_mode="hybrid",
        dim=2,
        embed_models=("model-a",),
        tenancy_sql="",
        tenancy_params=(),
        row_limit=row_limit,
        vector_kind=vector_kind,
    )
    return rows, has_vec, store


def test_vector_only_candidates_survive_a_full_keyword_side():
    kw = [f"kw-{i}" for i in range(10)]
    vec = [f"vec-{i}" for i in range(10)]
    rows, has_vec, _ = _fetch(kw, vec, row_limit=10)
    ids = [r["id"] for r in rows]
    assert has_vec is False
    assert set(ids) == set(kw) | set(vec), ids
    # Interleaved, so any later cap keeps both sources.
    assert {ids[0], ids[1]} == {"kw-0", "vec-0"}


def test_hydrate_restricts_to_the_embedder_identity():
    _, _, store = _fetch(["a"], ["b"], row_limit=5)
    sql, params = store["sql"], store["params"]
    assert "me.embed_model IN" in sql and "me.vector_kind =" in sql and "me.dim =" in sql
    assert "model-a" in params and "default" in params


def test_max_strategy_hydrates_every_kind():
    _, _, store = _fetch(["a"], ["b"], row_limit=5, vector_kind=None)
    assert "me.vector_kind" not in store["sql"]
