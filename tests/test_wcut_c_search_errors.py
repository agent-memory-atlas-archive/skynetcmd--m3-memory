"""The SQLite candidate fetch tells a rejected FTS query from a failing store.

Both fall back (semantic search, or empty for fts5 mode), but a locked,
malformed or schema-broken store was logged at DEBUG like a user's bad query,
so a broken store looked like "no keyword matches" with nothing in the log.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


def _err(match: str) -> sqlite3.OperationalError:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE f USING fts5(a)")
    try:
        c.execute("SELECT * FROM f WHERE f MATCH ?", (match,)).fetchall()
    except sqlite3.OperationalError as e:
        return e
    raise AssertionError(f"{match!r} did not raise")


@pytest.mark.parametrize("q", ['"abc', "a AND", "NEAR("])
def test_fts_parse_errors_are_query_errors(q):
    from memory.search import _is_fts_query_error

    assert _is_fts_query_error(_err(q), q)


def test_column_filter_is_a_query_error_only_when_the_query_names_it():
    from memory.search import _is_fts_query_error

    e = _err("x:y")
    assert _is_fts_query_error(e, "x:y")
    # The same message from a query that does not name the column is a schema
    # defect, not user input.
    assert not _is_fts_query_error(e, "plain words")
    assert not _is_fts_query_error(sqlite3.OperationalError("no such column: mi.foo"), "mi.foo:")


@pytest.mark.parametrize("msg", ["database is locked", "database disk image is malformed", "no such table: x"])
def test_store_failures_are_not_query_errors(msg):
    from memory.search import _is_fts_query_error

    assert not _is_fts_query_error(sqlite3.OperationalError(msg), "q")


@pytest.mark.asyncio
async def test_missing_fts_table_warns(monkeypatch, tmp_path, caplog):
    import memory_core
    from memory.config import EMBED_DIM

    from conftest import create_full_main_schema

    db = tmp_path / "t.db"
    create_full_main_schema(db)
    with sqlite3.connect(str(db)) as conn:
        for t in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")]:
            conn.execute(f"DROP TRIGGER IF EXISTS {t}")
        conn.execute("DROP TABLE memory_items_fts")

    @contextmanager
    def fake_db(existing=None, *a, **k):
        if existing is not None:
            yield existing
            return
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    async def fake_embed(_q):
        return ([1.0] + [0.0] * (EMBED_DIM - 1), "test-embed")

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_core, "_embed", fake_embed)
    with caplog.at_level(logging.WARNING, logger="memory.search"):
        await memory_core.memory_search_scored_impl(query="widget", k=3, mmr=False, search_mode="fts5")
    hits = [r for r in caplog.records if "candidate fetch failed" in r.getMessage()]
    assert hits, [r.getMessage() for r in caplog.records]
    assert "no such table" in hits[0].getMessage()
