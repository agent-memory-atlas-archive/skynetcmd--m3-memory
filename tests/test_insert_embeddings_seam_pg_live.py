"""`insert_embeddings` on a LIVE PostgreSQL cluster, with and without pgvector.

The SQLite half (test_insert_embeddings_seam.py) pins the blob contract. Here:
  * without pg_066's column, rows land exactly as on SQLite;
  * with it, every write also fills ``embedding_vec``, and the copy equals the
    blob float for float (the in-database search scores the copy, so a lossy copy
    would rank differently from the baseline path);
  * pg_066's trigger clears a copy that a writer left stale by changing only
    the blob, and leaves one a writer updated alongside it;
  * pg_066 down removes the column and writes keep working.

Each test runs pg_066's own SQL in a throwaway schema, so what is tested is the
shipped migration. The pgvector half skips where the extension is not available.
"""

from __future__ import annotations

import struct
import sys
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "bin"))

from conftest import skip_reason  # noqa: E402

pytestmark = pytest.mark.requires_pg

_MIG = _ROOT / "memory" / "migrations" / "postgres"


def _blob(floats):
    return struct.pack(f"{len(floats)}f", *floats)


@pytest.fixture()
def store(pg_url):
    """(backend, conn) with search_path on a fresh schema holding the two tables
    pg_066 touches. pgvector, if the server has it, is put in `public` first so
    the migration finds it installed rather than creating it in the schema."""
    from memory.backends.postgres_backend import PostgresBackend

    backend = PostgresBackend(dsn=pg_url)
    schema = f"t_emb_{uuid.uuid4().hex[:8]}"
    with backend.connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
            conn.commit()
        except Exception:  # noqa: BLE001 — not available / not permitted: the no-vector half still runs
            conn.rollback()
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}, public")
        cur.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY)")
        cur.execute(
            "CREATE TABLE memory_embeddings (id TEXT PRIMARY KEY, "
            "memory_id TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE, embedding BYTEA NOT NULL, "
            "embed_model TEXT DEFAULT 'jina-embeddings-v5', dim BIGINT DEFAULT 1024, "
            "created_at TIMESTAMPTZ DEFAULT NOW(), content_hash TEXT, vector_kind TEXT NOT NULL DEFAULT 'default')"
        )
        cur.execute("INSERT INTO memory_items VALUES ('m1'), ('m2')")
        conn.commit()
        try:
            yield backend, conn
        finally:
            conn.rollback()
            cur = conn.cursor()
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute("RESET search_path")
            conn.commit()
    backend.close()


def _migrate(conn, direction):
    cur = conn.cursor()
    cur.execute((_MIG / f"pg_066_pgvector_column.{direction}.sql").read_text(encoding="utf-8"))
    conn.commit()


def _has_column(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'memory_embeddings' AND column_name = 'embedding_vec'"
    )
    return cur.fetchone() is not None


def _vec(conn, eid):
    cur = conn.cursor()
    cur.execute("SELECT embedding_vec::text FROM memory_embeddings WHERE id = %s", (eid,))
    text = cur.fetchone()[0]
    return None if text is None else [float(x) for x in text.strip("[]").split(",")]


def _with_vector_column(conn):
    _migrate(conn, "up")
    if not _has_column(conn):
        pytest.skip(skip_reason("dep", "pgvector is not available on this PostgreSQL server"))


def test_without_the_column_rows_land_as_on_sqlite(store):
    from memory.backends.base import EmbeddingRow

    backend, conn = store
    assert not _has_column(conn)
    n = backend.insert_embeddings(conn, [EmbeddingRow("e1", "m1", _blob([0.5, -1.0]), "mod", 2, vector_kind="w0")])
    conn.commit()
    assert n == 1
    cur = conn.cursor()
    cur.execute("SELECT embedding, vector_kind, created_at IS NOT NULL FROM memory_embeddings")
    blob, kind, stamped = cur.fetchone()
    assert (bytes(blob), kind, stamped) == (_blob([0.5, -1.0]), "w0", True)


def test_the_vector_copy_equals_the_blob(store):
    from memory.backends.base import EmbeddingRow

    backend, conn = store
    _with_vector_column(conn)
    # Values a decimal round trip could get wrong if it went through float4 text badly.
    floats = [0.1, -0.3333333, 1e-30, 3.4e38, 0.0, -0.0, 123456.789]
    exact = list(struct.unpack(f"{len(floats)}f", _blob(floats)))
    backend.insert_embeddings(conn, [EmbeddingRow("e1", "m1", _blob(floats), "mod", len(floats))])
    conn.commit()
    got = _vec(conn, "e1")
    assert got is not None
    assert list(struct.unpack(f"{len(got)}f", _blob(got))) == exact


def test_upsert_refreshes_the_copy(store):
    from memory.backends.base import EmbeddingRow

    backend, conn = store
    _with_vector_column(conn)
    backend.insert_embeddings(conn, [EmbeddingRow("e1", "m1", _blob([1.0, 2.0]), "mod", 2)])
    backend.insert_embeddings(conn, [EmbeddingRow("e1", "m1", _blob([3.0, 4.0]), "mod", 2)], on_conflict="update")
    backend.insert_embeddings(conn, [EmbeddingRow("e1", "m1", _blob([9.0, 9.0]), "mod", 2)], on_conflict="ignore")
    conn.commit()
    assert _vec(conn, "e1") == [3.0, 4.0]


def test_trigger_clears_a_copy_left_stale(store):
    from memory.backends.base import EmbeddingRow

    backend, conn = store
    _with_vector_column(conn)
    backend.insert_embeddings(
        conn,
        [EmbeddingRow("e1", "m1", _blob([1.0, 2.0]), "mod", 2), EmbeddingRow("e2", "m2", _blob([1.0, 2.0]), "mod", 2)],
    )
    cur = conn.cursor()
    # A writer that changes only the blob: the copy must not survive it.
    cur.execute("UPDATE memory_embeddings SET embedding = %s WHERE id = 'e1'", (_blob([5.0, 6.0]),))
    # A writer that changes both: its copy stands.
    cur.execute(
        "UPDATE memory_embeddings SET embedding = %s, embedding_vec = '[5,6]' WHERE id = 'e2'", (_blob([5.0, 6.0]),)
    )
    # A change that leaves the blob alone keeps the copy.
    cur.execute("UPDATE memory_embeddings SET embed_model = 'other' WHERE id = 'e2'")
    conn.commit()
    assert _vec(conn, "e1") is None
    assert _vec(conn, "e2") == [5.0, 6.0]


def test_down_removes_the_column_and_writes_continue(store):
    from memory.backends.base import EmbeddingRow

    backend, conn = store
    _with_vector_column(conn)
    _migrate(conn, "down")
    assert not _has_column(conn)
    backend.insert_embeddings(conn, [EmbeddingRow("e1", "m1", _blob([1.0]), "mod", 1)])
    conn.commit()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM memory_embeddings")
    assert cur.fetchone()[0] == 1
