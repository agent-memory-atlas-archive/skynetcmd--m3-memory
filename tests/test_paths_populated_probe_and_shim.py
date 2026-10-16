"""paths: a locked live store is not "unpopulated"; the bootstrap shim commits.

_db_is_populated returned False on ANY error, so a live agent_memory.db held
locked by a writer lost to a populated legacy memory/agent_memory.db, splitting
reads and writes across two stores. The SQLite-only bootstrap shim's
connection() never committed, so writes through it vanished on close.
"""

from __future__ import annotations

import sqlite3

from m3_core import paths


def _populated(p):
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY)")
    c.commit()
    c.close()


def test_missing_and_schema_less_files_are_not_populated(tmp_path):
    assert paths._db_is_populated(str(tmp_path / "missing.db")) is False
    stub = tmp_path / "stub.db"
    sqlite3.connect(stub).close()
    assert paths._db_is_populated(str(stub)) is False
    assert not (tmp_path / "missing.db").exists(), "the probe must not create the file"


def test_unreadable_store_counts_as_populated_and_is_reported(tmp_path, caplog):
    junk = tmp_path / "agent_memory.db"
    junk.write_bytes(b"not a sqlite database at all, " * 100)
    with caplog.at_level("WARNING"):
        assert paths._db_is_populated(str(junk)) is True
    assert str(junk) in caplog.text


def test_locked_store_counts_as_populated(tmp_path):
    db = tmp_path / "agent_memory.db"
    _populated(db)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert paths._db_is_populated(str(db)) is True
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_bootstrap_shim_commits_and_names_rows(tmp_path):
    db = tmp_path / "s.db"
    shim = paths._sqlite_bootstrap_shim()
    with shim.connection(str(db)) as c:
        c.execute("CREATE TABLE t (k TEXT)")
        c.execute("INSERT INTO t VALUES ('kept')")
    with shim.connection(str(db)) as c:
        assert c.execute("SELECT k FROM t").fetchone()["k"] == "kept"


def test_bootstrap_shim_rolls_back_on_error(tmp_path):
    db = tmp_path / "s.db"
    shim = paths._sqlite_bootstrap_shim()
    with shim.connection(str(db)) as c:
        c.execute("CREATE TABLE t (k TEXT)")
    try:
        with shim.connection(str(db)) as c:
            c.execute("INSERT INTO t VALUES ('lost')")
            raise RuntimeError("caller failed")
    except RuntimeError:
        pass
    with shim.connection(str(db)) as c:
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
