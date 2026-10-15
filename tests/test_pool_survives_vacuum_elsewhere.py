"""A pooled connection works after another connection VACUUMs the store.

SQLite (3.46-3.53, measured 2026-10-15) does not reload the schema on a stale
connection for its first DELETE when an FTS5 external-content trigger and
foreign keys are involved: after a VACUUM elsewhere moved table pages, it
fails with "no such table". The nightly maintenance VACUUMs stores under
500 MB while the server's pool sits idle.
"""

from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE parent (id TEXT PRIMARY KEY, title TEXT, content TEXT);
CREATE VIRTUAL TABLE parent_fts USING fts5(title, content, content=parent, content_rowid=rowid);
CREATE TRIGGER p_fts_delete AFTER DELETE ON parent BEGIN
    INSERT INTO parent_fts(parent_fts, rowid, title, content) VALUES ('delete', old.rowid, old.title, old.content);
END;
CREATE TABLE filler (x);
CREATE TABLE child (pid TEXT REFERENCES parent(id) ON DELETE CASCADE);
INSERT INTO parent VALUES ('a', 't', 'c');
INSERT INTO parent_fts(rowid, title, content) SELECT rowid, title, content FROM parent;
DROP TABLE filler;
"""


def test_a_pooled_delete_after_a_vacuum_elsewhere(tmp_path):
    from m3_core.context import M3Context, _close_context_pool

    db = str(tmp_path / "v.db")
    with sqlite3.connect(db) as c:
        c.executescript(_SCHEMA)
    ctx = M3Context(db)
    try:
        with ctx.get_sqlite_conn() as conn:  # the pool opens and loads the schema
            assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 1
        other = sqlite3.connect(db)
        other.execute("VACUUM")  # moves `child`'s pages: the dropped table left a gap
        other.close()
        for _ in range(8):  # reach every pooled connection, not only the first
            with ctx.get_sqlite_conn() as conn:
                conn.execute("DELETE FROM parent WHERE id = 'missing'")
                conn.rollback()
        with ctx.get_sqlite_conn() as conn:
            conn.execute("DELETE FROM parent WHERE id = 'a'")
            conn.commit()
            assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 0
    finally:
        _close_context_pool(ctx)


def test_a_chatlog_pooled_delete_after_a_vacuum_elsewhere(tmp_path, monkeypatch):
    import chatlog_config as cc

    db = str(tmp_path / "chat.db")
    with sqlite3.connect(db) as c:
        c.executescript(_SCHEMA)
    monkeypatch.setattr(cc, "chatlog_db_path", lambda: db)
    monkeypatch.setattr(cc, "_POOL", None)
    with cc.chatlog_sqlite_conn() as conn:
        assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 1
    other = sqlite3.connect(db)
    other.execute("VACUUM")
    other.close()
    for _ in range(8):
        with cc.chatlog_sqlite_conn() as conn:
            conn.execute("DELETE FROM parent WHERE id = 'missing'")
            conn.rollback()
    pool = cc._POOL
    while pool is not None and not pool.empty():
        pool.get_nowait().close()
    monkeypatch.setattr(cc, "_POOL", None)
