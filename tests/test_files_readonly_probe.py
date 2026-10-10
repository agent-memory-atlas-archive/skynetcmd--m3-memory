"""files_memory.db.readonly_probe: read-only, quiet on "no store", loud on a defect.

It returned [] for ANY error, so a bad query looked exactly like "no files work"
and extraction silently never ran; and its SQLite connection was writable.
"""
from __future__ import annotations

import logging
import sqlite3


def _store(tmp_path):
    path = tmp_path / "files.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE leaves (uuid TEXT)")
        c.execute("INSERT INTO leaves VALUES ('a')")
    return str(path)


def test_probe_reads_and_a_missing_table_is_quiet(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    from files_memory.db import readonly_probe

    path = _store(tmp_path)
    rows = readonly_probe("SELECT uuid FROM leaves", db_path=path)
    assert [tuple(r) for r in rows] == [("a",)] and rows[0]["uuid"] == "a"
    with caplog.at_level(logging.WARNING, logger="files_memory.db"):
        assert readonly_probe("SELECT 1 FROM no_such_table_x", db_path=path) == []
    assert not caplog.records


def test_a_defect_is_a_warning_and_writes_are_refused(tmp_path, caplog, monkeypatch):
    """The SQLite FILE branch (PG routes through the pool). The SQLite backend is
    loaded first: log_swallowed classifies through backends already loaded."""
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    from memory.backends import active_backend

    assert active_backend().name == "sqlite"
    from files_memory.db import readonly_probe

    path = _store(tmp_path)
    with caplog.at_level(logging.WARNING, logger="files_memory.db"):
        assert readonly_probe("SELECT no_such_column_x FROM leaves", db_path=path) == []
        assert readonly_probe("INSERT INTO leaves VALUES ('b')", db_path=path) == []
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 2 and all("observed:" in w for w in warns), warns
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM leaves").fetchone()[0] == 1, "the probe wrote"
