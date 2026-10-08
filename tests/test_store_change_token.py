"""Pins `Dialect.store_change_token()`, the seam's connection-free change signal.

The notification waiter wakes on this token instead of querying every tick, so
the one property that matters is: it must NEVER stay equal across a committed
write. It must also stay connection-free and never create the store, because
the waiter calls it before a schema (or, on PostgreSQL, a DSN) exists.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends.dialect import Dialect, dialect_for  # noqa: E402


def test_sqlite_token_moves_on_every_commit_in_wal_mode(tmp_path):
    db = str(tmp_path / "s.db")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (x)")
    con.commit()
    d = dialect_for("sqlite")
    try:
        seen = {d.store_change_token(db)}
        for i in range(5):
            con.execute("INSERT INTO t VALUES (?)", (i,))
            con.commit()
            tok = d.store_change_token(db)
            assert tok not in seen, f"commit {i} left the token unchanged: {tok}"
            seen.add(tok)
    finally:
        con.close()


def test_sqlite_token_moves_on_a_commit_in_rollback_journal_mode(tmp_path):
    """No -wal file at all: the main file must carry the signal."""
    db = str(tmp_path / "j.db")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE t (x)")
    con.commit()
    d = dialect_for("sqlite")
    try:
        before = d.store_change_token(db)
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        assert d.store_change_token(db) != before
    finally:
        con.close()


def test_sqlite_token_is_stable_without_a_write(tmp_path):
    db = str(tmp_path / "q.db")
    sqlite3.connect(db).close()
    d = dialect_for("sqlite")
    assert d.store_change_token(db) == d.store_change_token(db)


def test_sqlite_token_never_creates_the_store(tmp_path):
    db = tmp_path / "absent.db"
    assert dialect_for("sqlite").store_change_token(str(db)) == ((), ())
    assert not db.exists(), "a change probe created the store it was watching"


def test_postgres_has_no_connection_free_signal():
    """None is the capability answer the waiter branches on; the argument is not
    a DSN and must not be parsed or connected to."""
    assert dialect_for("postgres").store_change_token("not-a-dsn") is None


def test_a_new_backend_must_choose_explicitly():
    """The base raises so a third backend cannot silently inherit SQLite's file
    signal and watch files it never writes."""
    with pytest.raises(NotImplementedError):
        Dialect.store_change_token(object(), "x")  # type: ignore[arg-type]
