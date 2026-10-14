"""`insert_embeddings` — the one write path for memory_embeddings rows (SQLite half).

Every writer hands rows to the backend instead of spelling its own INSERT, so a
backend that keeps more than the blob (PostgreSQL's pgvector copy) can never be
bypassed. The PostgreSQL half, including the vector copy and pg_066's
invalidation trigger, is test_insert_embeddings_seam_pg_live.py.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

from memory.backends.base import EmbeddingRow, embedding_row_groups  # noqa: E402
from memory.backends.sqlite_backend import SqliteBackend  # noqa: E402


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY)")
    c.execute(
        "CREATE TABLE memory_embeddings (id TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memory_items(id), "
        "embedding BLOB NOT NULL, embed_model TEXT DEFAULT 'jina-embeddings-v5', dim INTEGER DEFAULT 1024, "
        "created_at TEXT DEFAULT 'DEFAULTED', content_hash TEXT, vector_kind TEXT NOT NULL DEFAULT 'default')"
    )
    c.executemany("INSERT INTO memory_items VALUES (?)", [("m1",), ("m2",)])
    yield c
    c.close()


def _rows(c):
    return {
        r[0]: r[1:]
        for r in c.execute(
            "SELECT id, embedding, embed_model, created_at, content_hash, vector_kind FROM memory_embeddings"
        )
    }


def test_unset_optional_columns_take_the_table_default(conn):
    n = SqliteBackend().insert_embeddings(
        conn,
        [
            EmbeddingRow("e1", "m1", b"\x00" * 8, "mod", 2, "2026-01-01", "h1", "window_0"),
            EmbeddingRow("e2", "m2", b"\x01" * 8, "mod", 2),
        ],
    )
    assert n == 2
    rows = _rows(conn)
    assert rows["e1"] == (b"\x00" * 8, "mod", "2026-01-01", "h1", "window_0")
    # Not NULL: a row that leaves created_at / vector_kind unset gets the defaults.
    assert rows["e2"] == (b"\x01" * 8, "mod", "DEFAULTED", None, "default")


def test_conflict_modes(conn):
    b = SqliteBackend()
    b.insert_embeddings(conn, [EmbeddingRow("e1", "m1", b"old!", "a", 1)])
    with pytest.raises(sqlite3.IntegrityError):
        b.insert_embeddings(conn, [EmbeddingRow("e1", "m1", b"new!", "b", 1)])
    b.insert_embeddings(conn, [EmbeddingRow("e1", "m1", b"new!", "b", 1)], on_conflict="ignore")
    assert _rows(conn)["e1"][:2] == (b"old!", "a")
    b.insert_embeddings(conn, [EmbeddingRow("e1", "m1", b"new!", "b", 1)], on_conflict="update")
    assert _rows(conn)["e1"][:2] == (b"new!", "b")


def test_no_rows_is_a_no_op(conn):
    assert SqliteBackend().insert_embeddings(conn, []) == 0


def test_groups_keep_order_and_split_by_shape():
    rows = [
        EmbeddingRow("a", "m", b"", "x", 0, content_hash="h"),
        EmbeddingRow("b", "m", b"", "x", 0),
        EmbeddingRow("c", "m", b"", "x", 0, content_hash="h"),
    ]
    groups = embedding_row_groups(rows)
    assert [(cols[-1], [v[0] for v in vals]) for cols, vals in groups] == [("content_hash", ["a", "c"]), ("dim", ["b"])]


def test_sqlite_keeps_no_vector_copy(conn):
    b = SqliteBackend()
    assert b.vector_copy_status(conn) is None
    assert b.ensure_vector_copy(conn) is False
    assert b.fill_vector_copy(conn) == 0


def test_the_lacks_copy_predicate_matches_the_partial_index():
    """The planner uses pg_066's partial index only for a query whose WHERE is the
    index's own predicate; a drifted copy would turn every readiness check into a
    full scan with no error anywhere."""
    from memory.backends.postgres_backend import _LACKS_COPY

    up = (_BIN.parent / "memory" / "migrations" / "postgres" / "pg_066_pgvector_column.up.sql").read_text(
        encoding="utf-8"
    )
    index_wheres = [
        line.split("WHERE", 1)[1].strip().rstrip(";") for line in up.splitlines() if "WHERE embedding" in line
    ]
    assert len(index_wheres) == 2  # the core table and the chat log clone
    assert all(w == _LACKS_COPY for w in index_wheres)
