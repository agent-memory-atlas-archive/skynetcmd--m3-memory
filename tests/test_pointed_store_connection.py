"""`m3_core.context.pointed_store_connection`: a tool's connection to the store
it was POINTED at -- never migrated, committed or rolled back on both backends.

memory.db._db() runs _lazy_init on the first touch of a path, which shells out
to the migration chain: a backfill pointed at a bench workspace or an export
would silently migrate it. The bare SQLite pool returns a connection as-is, so
a missed commit left an open transaction for its next user.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from conftest import use_backend


def test_a_pointed_sqlite_file_is_not_migrated(tmp_path, monkeypatch):
    from m3_core.context import pointed_store_connection

    use_backend("sqlite")
    path = tmp_path / "export.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE only_this (x INTEGER)")
    raw.commit()
    raw.close()

    with pointed_store_connection(str(path)) as conn:
        conn.execute("INSERT INTO only_this VALUES (1)")

    raw = sqlite3.connect(path)
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = raw.execute("SELECT x FROM only_this").fetchall()
    raw.close()
    assert tables == {"only_this"}, f"observed: the pointed file was migrated: {sorted(tables)}"
    assert rows == [(1,)], f"observed: a clean exit did not commit: {rows!r}"


def test_the_pointed_file_wins_over_m3_database(tmp_path, monkeypatch):
    """resolve_db_path ranks M3_DATABASE above active_database, so the earlier
    `active_database(path) + seam connection` form acted on the MAIN store
    whenever M3_DATABASE was set: `m3_enrich_assign --db X` updated the wrong
    file. The sandbox sets M3_DATABASE to the target, which hid it."""
    from m3_core.context import pointed_store_connection

    use_backend("sqlite")
    target, other = tmp_path / "target.db", tmp_path / "main.db"
    for p in (target, other):
        raw = sqlite3.connect(p)
        raw.execute("CREATE TABLE t (x INTEGER)")
        raw.commit()
        raw.close()
    monkeypatch.setenv("M3_DATABASE", str(other))

    with pointed_store_connection(str(target)) as conn:
        conn.execute("INSERT INTO t VALUES (7)")

    got = {p.name: sqlite3.connect(p).execute("SELECT count(*) FROM t").fetchone()[0] for p in (target, other)}
    assert got == {"target.db": 1, "main.db": 0}, f"observed: {got!r}"


def test_an_error_rolls_back(each_backend):
    from m3_core.context import pointed_store_connection
    from m3_sdk import resolve_db_path
    from memory.backends import dialect

    with each_backend.connection():
        pass  # the sandbox materializes the migrated store on first touch
    target = resolve_db_path(None) if each_backend.stores_databases_as_files else None
    p = dialect().param()
    key = f"PSC_{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError, match="boom"):
        with pointed_store_connection(target) as conn:
            conn.execute(
                f"INSERT INTO synchronized_secrets (service_name, encrypted_value) VALUES ({p}, {p})", (key, "x")
            )
            raise RuntimeError("boom")
    with each_backend.connection() as conn:
        left = conn.execute(f"SELECT count(*) FROM synchronized_secrets WHERE service_name = {p}", (key,)).fetchone()[0]
        conn.execute(f"DELETE FROM synchronized_secrets WHERE service_name = {p}", (key,))  # never leave a row
        conn.commit()
    assert left == 0, f"observed: the failed block's write survived on {each_backend.name}"


def test_no_path_on_a_file_backend_is_refused(monkeypatch):
    from m3_core.context import pointed_store_connection

    use_backend("sqlite")
    with pytest.raises(ValueError, match="observed: no SQLite path"):
        with pointed_store_connection(None):
            pass
