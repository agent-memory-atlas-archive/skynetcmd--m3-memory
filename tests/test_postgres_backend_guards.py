"""PostgreSQL backend guards that used to fail silently.

- _reject_same_as_warehouse compared (host, port, dbname) tuples for equality, so
  a socket DSN vs `localhost`, or a DSN with no dbname, "proved" two spellings of
  one database different and let the primary BE the warehouse.
- a failure reading the warehouse DSN for that guard returned "" with no word,
  silently disabling the guard.
- an advisory-unlock failure after the schema migration returned the session,
  still holding the lock, to the pool: every other process then blocked.

Hermetic: no cluster; the pool and connection are fakes.
"""

from __future__ import annotations

import importlib

import pytest


def _pgb():
    return importlib.import_module("memory.backends.postgres_backend")


@pytest.mark.parametrize(
    ("primary", "warehouse"),
    [
        ("postgresql:///m3", "postgresql://localhost/m3"),
        ("postgresql://u@127.0.0.1:5432/m3", "host=localhost dbname=m3 user=other"),
        ("postgresql://u@db.lan/m3", "postgresql://db.lan"),  # no dbname, no user: cannot prove different
        ("postgresql://u@db.lan/m3", "postgresql://m3@db.lan"),  # libpq: dbname defaults to the user
    ],
)
def test_two_spellings_of_one_database_are_refused(monkeypatch, primary, warehouse):
    pgb = _pgb()
    monkeypatch.setattr(pgb, "_resolve_warehouse_dsn_for_guard", lambda: warehouse)
    with pytest.raises(RuntimeError, match="SAME database"):
        pgb._reject_same_as_warehouse(primary)


@pytest.mark.parametrize(
    ("primary", "warehouse"),
    [
        ("postgresql://u@db.lan/m3", "postgresql://u@db.lan/m3_warehouse"),
        ("postgresql://u@db.lan/m3", "postgresql://u@other.lan/m3"),
        ("postgresql://u@db.lan:5432/m3", "postgresql://u@db.lan:5433/m3"),
    ],
)
def test_provably_different_databases_are_allowed(monkeypatch, primary, warehouse):
    pgb = _pgb()
    monkeypatch.setattr(pgb, "_resolve_warehouse_dsn_for_guard", lambda: warehouse)
    pgb._reject_same_as_warehouse(primary)


def test_warehouse_lookup_failure_is_reported_not_silent(monkeypatch, caplog):
    pgb = _pgb()
    ss = importlib.import_module("m3_core.secret_store")

    def _boom(role):
        raise OSError("keyring backend unavailable")

    monkeypatch.setattr(ss, "resolve_pg_dsn", _boom)
    with caplog.at_level("WARNING"):
        assert pgb._resolve_warehouse_dsn_for_guard() == ""
    assert "keyring backend unavailable" in caplog.text and "SKIPPED" in caplog.text


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        if "RESET search_path" in sql:
            raise RuntimeError("schema step failed")
        if "pg_advisory_unlock" in sql:
            raise RuntimeError("connection lost before unlock")


class _Conn:
    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass

    def rollback(self):
        pass


class _Pool:
    def __init__(self):
        self.returned: list[tuple] = []

    def getconn(self):
        return _Conn()

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))


def test_failed_unlock_closes_the_session_instead_of_pooling_it(caplog):
    pgb = _pgb()
    b = pgb.PostgresBackend(dsn="postgresql://u@db.invalid/m3")
    pool = _Pool()
    b._pool = pool
    with caplog.at_level("WARNING"), pytest.raises(RuntimeError, match="schema step failed"):
        b.ensure_schema()
    assert len(pool.returned) == 1 and pool.returned[0][1] is True, pool.returned
    assert "advisory lock" in caplog.text and "db.invalid" in caplog.text
