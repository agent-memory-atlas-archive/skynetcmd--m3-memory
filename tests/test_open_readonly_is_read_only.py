"""`open_readonly` means read-only on EVERY backend.

On SQLite it opens `mode=ro`; on PostgreSQL it yielded a writable pooled
connection, so a write through it failed on one backend and silently succeeded on
the other. The pool must also get the connection back writable.
"""

from __future__ import annotations

import pytest


def _existing_store(backend) -> str:
    """The sandbox store's path, created first: mode=ro cannot create a file."""
    from m3_sdk import resolve_db_path

    with backend.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return str(resolve_db_path(None))


def test_a_write_through_open_readonly_is_refused(each_backend):
    path = _existing_store(each_backend)
    with pytest.raises(Exception) as err:
        with each_backend.open_readonly(path) as conn:
            conn.execute("CREATE TABLE ro_probe_x (id INTEGER)")
    assert "read" in str(err.value).lower(), f"observed: {err.value!r}. inspect: open_readonly"


def test_the_pool_gets_a_writable_connection_back(each_backend):
    with each_backend.open_readonly(_existing_store(each_backend)) as conn:
        conn.execute("SELECT 1").fetchone()
    with each_backend.connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ro_probe_after (id INTEGER)")
        conn.execute("DROP TABLE ro_probe_after")
