"""M3Context's connection helpers fail loudly and name the store.

- pg_connection retried around the YIELD, so a caller's own OperationalError
  re-entered the generator ("generator didn't stop after throw()").
- get_chatlog_conn caught any error building the backend and fell back to local
  SQLite: a PostgreSQL install without a DSN wrote chat turns to a file.
- get_dispatch_conn fell back to the MAIN store when the dispatch path did not
  resolve, so dispatch rows landed where no reader looks.
- an exhausted SQLite pool raised a bare queue.Empty naming nothing.
"""

from __future__ import annotations

import importlib

import psycopg2
import pytest


def _ctx(tmp_path):
    from m3_core.context import M3Context

    return M3Context(str(tmp_path / "agent_memory.db"))


class _Conn:
    closed = False

    def close(self):
        self.closed = True


def test_a_callers_operational_error_is_not_retried(tmp_path, monkeypatch):
    from m3_core.secret_store import set_secret

    set_secret("M3_CDW_PG_URL", "postgresql://u@wh.invalid/w")
    pgconn = importlib.import_module("m3_core.pgconn")
    made: list[_Conn] = []

    def _connect(url, **kw):
        made.append(_Conn())
        return made[-1]

    monkeypatch.setattr(pgconn, "pg_connect", _connect)
    ctx = _ctx(tmp_path)
    with pytest.raises(psycopg2.OperationalError, match="mid-body"):
        with ctx.pg_connection():
            raise psycopg2.OperationalError("mid-body failure")
    assert len(made) == 1, "the body's error must not trigger a reconnect"
    assert made[0].closed


def test_connect_failures_are_retried_then_named(tmp_path, monkeypatch):
    from m3_core.secret_store import set_secret

    set_secret("M3_CDW_PG_URL", "postgresql://u:hunter2@wh.invalid/w")
    pgconn = importlib.import_module("m3_core.pgconn")
    calls = []

    def _refuse(url, **kw):
        calls.append(url)
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(pgconn, "pg_connect", _refuse)
    monkeypatch.setattr(importlib.import_module("m3_core.context").time, "sleep", lambda s: None)
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError) as ei:
        with ctx.pg_connection():
            pass
    assert len(calls) == 2
    msg = str(ei.value)
    assert "wh.invalid" in msg and "hunter2" not in msg


def test_missing_warehouse_dsn_names_the_real_command(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="m3 secret set M3_CDW_PG_URL"):
        with ctx.pg_connection():
            pass


def test_chatlog_on_postgres_without_a_dsn_raises_instead_of_using_sqlite(tmp_path, monkeypatch):
    from conftest import select_backend

    ctx = _ctx(tmp_path)
    select_backend("postgres")
    mb = importlib.import_module("memory.backends")

    def _no_dsn():
        raise RuntimeError("no primary DSN")

    monkeypatch.setattr(mb, "active_backend", _no_dsn)
    with pytest.raises(RuntimeError, match="no primary DSN"):
        with ctx.get_chatlog_conn():
            pass


def test_unresolvable_dispatch_path_raises_instead_of_writing_main(tmp_path, monkeypatch):
    paths = importlib.import_module("m3_core.paths")

    def _boom():
        raise OSError("engine root unreadable")

    monkeypatch.setattr(paths, "resolve_dispatch_db", _boom)
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError, match="M3_DISPATCH_DB") as ei:
        with ctx.get_dispatch_conn():
            pass
    assert "engine root unreadable" in str(ei.value)


def test_exhausted_pool_names_the_store_and_the_setting(tmp_path, monkeypatch):
    import queue

    ctx = _ctx(tmp_path)

    class _Q(queue.Queue):  # every connection checked out
        def get(self, block=True, timeout=None):
            raise queue.Empty

    ctx._pool = _Q(maxsize=1)
    with pytest.raises(RuntimeError) as ei:
        with ctx.get_sqlite_conn():
            pass
    msg = str(ei.value)
    assert "agent_memory.db" in msg and "M3_DB_POOL_SIZE" in msg and "pool size 1" in msg
