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

# Same-size commits, many of them. A file-stat token misses ~25-30% of these
# (mtime moves at the clock tick, and a same-size write keeps the size), so a
# handful of commits passes by luck: the old 1-5 commit versions of these tests
# were flaky, not green. At 200 commits a stat-only token fails with near
# certainty, which is what lets these tests catch that regression.
_COMMITS = 200


def _assert_every_commit_moves(con, db, write):
    d = dialect_for("sqlite")
    for i in range(_COMMITS):
        before = d.store_change_token(db)
        write(con, i)
        con.commit()
        assert d.store_change_token(db) != before, f"commit {i} left the token unchanged"


def test_sqlite_token_moves_on_every_commit_in_wal_mode(tmp_path):
    db = str(tmp_path / "s.db")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    # Checkpoint every 2 pages, so commits OVERWRITE reused frames at constant
    # -wal size: the long-running store's steady state. Without it each commit
    # appends, the size grows, and a stat-only token passes this test.
    con.execute("PRAGMA wal_autocheckpoint=2")
    con.execute("CREATE TABLE t (x)")
    # Start at 'z' so every UPDATE changes the value: SQLite writes no WAL frame
    # for a same-value UPDATE (measured), which is no write, so an unchanged
    # token there would be correct, not a miss.
    con.execute("INSERT INTO t VALUES ('z')")
    con.commit()
    try:
        _assert_every_commit_moves(con, db, lambda c, i: c.execute("UPDATE t SET x = ?", ("ab"[i % 2],)))
    finally:
        con.close()


def test_sqlite_token_moves_on_a_commit_in_rollback_journal_mode(tmp_path):
    """No -wal file at all: the main file must carry the signal."""
    db = str(tmp_path / "j.db")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("CREATE TABLE t (x)")
    con.commit()
    try:
        _assert_every_commit_moves(con, db, lambda c, i: c.execute("INSERT INTO t VALUES (?)", (i,)))
    finally:
        con.close()


def test_sqlite_token_is_stable_without_a_write(tmp_path):
    db = str(tmp_path / "q.db")
    sqlite3.connect(db).close()
    d = dialect_for("sqlite")
    assert d.store_change_token(db) == d.store_change_token(db)


def test_sqlite_token_never_creates_the_store(tmp_path):
    db = tmp_path / "absent.db"
    assert dialect_for("sqlite").store_change_token(str(db)) == ((), (), ())
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
