"""A failed commit is not a write.

flush_entity_vectors and build_bypass_surface each swallowed a failing
db.commit() and still reported the rows as written. The flush stays
best-effort (its contract says it never raises) but returns 0 and warns; the
builder, which makes no such promise, raises.
"""
from __future__ import annotations

import contextlib
import importlib
import logging
import sqlite3

import pytest
from test_bypass_surface import built_db  # noqa: F401 — the seeded-DB fixture, used by name


class _CommitFails:
    """A connection whose commit raises; everything else is the real one."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def commit(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _vec_table() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE entity_embeddings (entity_id TEXT PRIMARY KEY, "
                 "embedding BLOB, embed_model TEXT, dim INTEGER)")
    return conn


def test_flush_counts_nothing_when_the_commit_fails(caplog):
    me = importlib.import_module("memory.entity")
    buf = [("e1", [0.1, 0.2], "m"), ("e2", [0.3, 0.4], "m")]
    with caplog.at_level(logging.WARNING):
        n = me.flush_entity_vectors(_CommitFails(_vec_table()), buf)
    assert n == 0
    assert "2 rows not written" in caplog.text


def test_flush_counts_only_rows_it_sent():
    me = importlib.import_module("memory.entity")
    conn = _vec_table()
    n = me.flush_entity_vectors(conn, [("e1", [0.1, 0.2], "m"), ("e2", None, "m")])
    assert n == 1
    assert conn.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0] == 1


def test_bypass_build_raises_when_the_commit_fails(request, monkeypatch):
    E, db = request.getfixturevalue("built_db")

    @contextlib.contextmanager
    def failing_db():
        yield _CommitFails(db)

    monkeypatch.setattr(E, "_db", failing_db)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        E.build_bypass_surface(strategy_for={"convA": "COMPUTE", "convB": "COMPUTE"},
                               category_for={"convA": "person", "convB": "person"},
                               type_rules={"person": ("person",)})

