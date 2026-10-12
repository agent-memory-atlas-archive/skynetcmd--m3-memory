"""M3Context.query_memory refuses a write instead of silently losing it.

It never commits; a mutating statement used to vanish when the connection went
back to the pool. PRAGMA query_only now makes it raise, and the pool gets the
connection back writable.
"""

from __future__ import annotations

import sqlite3

import pytest


def test_query_memory_reads_refuses_writes_and_leaves_the_pool_writable(tmp_path, monkeypatch):
    from m3_core.context import M3Context, _close_context_pool

    ctx = M3Context(str(tmp_path / "q.db"))
    try:
        assert ctx.query_memory("SELECT 1")[0][0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ctx.query_memory("CREATE TABLE q_probe (id INTEGER)")
        with ctx.get_sqlite_conn() as conn:
            conn.execute("CREATE TABLE q_after (id INTEGER)")
            conn.commit()
        assert ctx.query_memory("SELECT name FROM sqlite_master WHERE name = 'q_after'")
    finally:
        _close_context_pool(ctx)
