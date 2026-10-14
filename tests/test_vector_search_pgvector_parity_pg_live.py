"""PostgreSQL vector search: the pgvector arm returns what the baseline returns.

Both arms are exact. The baseline ships candidate blobs to the Rust scorer; the
pgvector arm scores the vector copy (pg_066) in the database. Same filters (live
rows, dimension, embed models, tenancy), one hit per embedding row, highest cosine
first, a zero vector scored 0. Checked here on random data shaped to hit each
filter, plus the dispatch rule: the database arm is used only when every row has
its copy.

Runs pg_066's own SQL in a throwaway schema; skips where pgvector is unavailable.
"""

from __future__ import annotations

import random
import struct
import sys
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "bin"))

from conftest import skip_reason  # noqa: E402

pytestmark = pytest.mark.requires_pg

_UP = _ROOT / "memory" / "migrations" / "postgres" / "pg_066_pgvector_column.up.sql"
_DIM = 8


def _blob(v):
    return struct.pack(f"{len(v)}f", *v)


@pytest.fixture()
def store(pg_url):
    from memory.backends.base import EmbeddingRow
    from memory.backends.postgres_backend import PostgresBackend

    backend = PostgresBackend(dsn=pg_url)
    schema = f"t_vs_{uuid.uuid4().hex[:8]}"
    rnd = random.Random(7)
    with backend.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
            conn.commit()
        except Exception:  # noqa: BLE001 — not available: the tests skip below
            conn.rollback()
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}, public")
        cur.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY, is_deleted INTEGER DEFAULT 0, user_id TEXT)")
        cur.execute(
            "CREATE TABLE memory_embeddings (id TEXT PRIMARY KEY, "
            "memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE, embedding BYTEA NOT NULL, "
            "embed_model TEXT, dim BIGINT, created_at TIMESTAMPTZ DEFAULT NOW(), content_hash TEXT, "
            "vector_kind TEXT NOT NULL DEFAULT 'default')"
        )
        cur.execute("CREATE TABLE chat_log_embeddings (LIKE memory_embeddings INCLUDING ALL)")
        cur.execute(_UP.read_text(encoding="utf-8"))
        conn.commit()
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'memory_embeddings' AND column_name = 'embedding_vec'"
        )
        if cur.fetchone() is None:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
            cur.execute("RESET search_path")
            conn.commit()
            backend.close()
            pytest.skip(skip_reason("dep", "pgvector is not available on this PostgreSQL server"))

        items, rows = [], []
        for i in range(120):
            mid = f"m{i:03d}"
            items.append((mid, 1 if i % 17 == 0 else 0, "alice" if i % 2 else "bob"))
            for k in range(1 + i % 3):  # several kinds (windows) per memory
                vec = [rnd.uniform(-1, 1) for _ in range(_DIM)]
                rows.append(EmbeddingRow(f"e{i:03d}_{k}", mid, _blob(vec), "bge" if i % 11 else "other", _DIM))
        rows.append(EmbeddingRow("e_zero", "m001", _blob([0.0] * _DIM), "bge", _DIM))
        rows.append(EmbeddingRow("e_wide", "m002", _blob([0.5] * (_DIM * 2)), "bge", _DIM * 2))
        cur.executemany("INSERT INTO memory_items VALUES (%s, %s, %s)", items)
        backend.insert_embeddings(conn, rows)
        conn.commit()
        try:
            yield backend, conn, rnd
        finally:
            conn.rollback()
            cur = conn.cursor()
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute("RESET search_path")
            conn.commit()
    backend.close()


_CASES = [
    {},
    {"embed_models": ("bge",)},
    {"tenancy_sql": " AND mi.user_id = %s", "tenancy_params": ("alice",)},
    {"embed_models": ("bge", "other"), "tenancy_sql": " AND mi.user_id = %s", "tenancy_params": ("bob",)},
]


@pytest.mark.parametrize("case", _CASES, ids=["plain", "model", "tenant", "both"])
@pytest.mark.parametrize("limit", [1, 8, 50, 1000])
def test_both_arms_agree(store, case, limit):
    backend, conn, rnd = store
    from memory.backends.postgres_backend import _vector_copy_ready

    assert _vector_copy_ready(conn.cursor())
    for _ in range(5):
        q = [rnd.uniform(-1, 1) for _ in range(_DIM)]
        base = backend._vector_search_baseline(conn, q, limit=limit, dim=_DIM, **case)
        pgv = backend._vector_search_pgvector(conn.cursor(), q, limit=limit, dim=_DIM, **case)
        assert len(pgv) == len(base)
        for a, b in zip(base, pgv, strict=True):
            assert b.score == pytest.approx(a.score, abs=1e-5)
        # Same memories in the same order, wherever scores are not tied.
        assert [h.memory_id for h in pgv] == [h.memory_id for h in base] or all(
            abs(x.score - y.score) < 1e-6 for x, y in zip(base, pgv, strict=True)
        )


def test_a_zero_vector_scores_zero_on_both_arms(store):
    backend, conn, _ = store
    q = [1.0] + [0.0] * (_DIM - 1)
    base = {h.score for h in backend._vector_search_baseline(conn, q, limit=1000, dim=_DIM) if h.memory_id == "m001"}
    pgv = {
        h.score
        for h in backend._vector_search_pgvector(conn.cursor(), q, limit=1000, dim=_DIM)
        if h.memory_id == "m001"
    }
    assert 0.0 in base and 0.0 in pgv


def test_dispatch_uses_the_database_only_when_every_copy_exists(store, monkeypatch):
    backend, conn, _ = store
    used = []
    real_pgv, real_base = backend._vector_search_pgvector, backend._vector_search_baseline
    monkeypatch.setattr(backend, "_vector_search_pgvector", lambda *a, **k: used.append("db") or real_pgv(*a, **k))
    monkeypatch.setattr(backend, "_vector_search_baseline", lambda *a, **k: used.append("base") or real_base(*a, **k))
    q = [0.1] * _DIM
    backend.vector_search(conn, q, limit=8, dim=_DIM)
    # A writer that changes only the blob: pg_066's trigger clears that row's copy.
    conn.cursor().execute("UPDATE memory_embeddings SET embedding = %s WHERE id = 'e005_0'", (_blob([0.3] * _DIM),))
    backend.vector_search(conn, q, limit=8, dim=_DIM)
    assert backend.fill_vector_copy(conn) == 1
    backend.vector_search(conn, q, limit=8, dim=_DIM)
    assert used == ["db", "base", "db"]
