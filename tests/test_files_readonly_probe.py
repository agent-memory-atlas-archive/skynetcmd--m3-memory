"""files_memory.db.readonly_probe: read-only, quiet on "no store", loud on a defect,
on EVERY backend.

It returned [] for ANY error, so a bad query looked exactly like "no files work"
and extraction silently never ran; and its SQLite connection was writable. The
store is built by the files layer itself (init_db runs that backend's
migrations) and named through files_table(), so the test speaks no dialect.
"""
from __future__ import annotations

import logging


def _store(tmp_path) -> str:
    from files_memory.db import init_db

    path = str(tmp_path / "files.db")
    init_db(path)
    return path


def test_probe_reads_and_a_missing_table_is_quiet(tmp_path, caplog, each_backend):
    from files_memory.config import files_table
    from files_memory.db import readonly_probe

    path = _store(tmp_path)
    rows = readonly_probe(f"SELECT count(*) FROM {files_table('leaves')}", db_path=path)
    assert len(rows) == 1, f"observed: {rows!r} on {each_backend.name}. inspect: readonly_probe"
    with caplog.at_level(logging.WARNING, logger="files_memory.db"):
        assert readonly_probe(f"SELECT 1 FROM {files_table('no_such_table_x')}", db_path=path) == []
    assert not caplog.records, [r.getMessage() for r in caplog.records]


def test_a_defect_is_a_warning_and_a_write_is_refused(tmp_path, caplog, each_backend):
    from files_memory.config import files_table
    from files_memory.db import readonly_probe

    path = _store(tmp_path)
    with caplog.at_level(logging.WARNING, logger="files_memory.db"):
        assert readonly_probe(f"SELECT no_such_column_x FROM {files_table('leaves')}", db_path=path) == []
        assert readonly_probe(f"CREATE TABLE {files_table('ro_probe_x')} (id INTEGER)", db_path=path) == []
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 2 and all("observed:" in w for w in warns), (each_backend.name, warns)
    assert readonly_probe(f"SELECT 1 FROM {files_table('ro_probe_x')}", db_path=path) == [], (
        f"observed: the probe created a table on {each_backend.name}")
