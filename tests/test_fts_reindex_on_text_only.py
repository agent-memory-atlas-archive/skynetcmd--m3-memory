"""The FTS index is rewritten only when a memory's text changes.

mi_fts_update fired on an update of ANY column, so every search's access stamps
and every decay or grading pass rewrote FTS entries; the merges that followed
stalled searches for up to ~240 ms on a live store (measured 2026-10-15). Both stores carry the trigger.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _chatlog_store(path):
    conn = sqlite3.connect(str(path))
    for f in sorted((_ROOT / "memory" / "chatlog_migrations").glob("*.up.sql")):
        conn.executescript(f.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def _main_store(path):
    from conftest import create_full_main_schema

    create_full_main_schema(path)
    return sqlite3.connect(str(path))


@pytest.mark.parametrize("make_store", [_main_store, _chatlog_store], ids=["main", "chatlog"])
def test_only_a_text_change_reindexes(tmp_path, make_store):
    conn = make_store(tmp_path / "s.db")
    conn.execute("INSERT INTO memory_items (id, type, title, content) VALUES ('a', 'note', 'old title', 'body')")
    conn.commit()

    def index_blocks():
        # The FTS table's own storage: a reindex writes new blocks. FTS5 holds
        # pending writes in memory until COMMIT, so each update commits first.
        conn.commit()
        return conn.execute("SELECT id, block FROM memory_items_fts_data ORDER BY id").fetchall()

    before = index_blocks()
    conn.execute("UPDATE memory_items SET access_count = 7, importance = 0.3 WHERE id = 'a'")
    assert index_blocks() == before, "a counter update must not rewrite the FTS entry"

    conn.execute("UPDATE memory_items SET title = 'new heading' WHERE id = 'a'")
    assert index_blocks() != before, "a text change must reindex"
    match = "SELECT count(*) FROM memory_items_fts WHERE memory_items_fts MATCH ?"
    assert conn.execute(match, ("heading",)).fetchone()[0] == 1
    assert conn.execute(match, ("old",)).fetchone()[0] == 0
    conn.close()
