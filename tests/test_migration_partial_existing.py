"""A migration whose ADD COLUMN already exists still applies its other statements.

The runner used to roll the WHOLE file back on "duplicate column", then mark the
version applied: every other statement in the file was silently dropped (it cost
main 038 its index on stores a runtime fallback had already given the column).
"""

from __future__ import annotations

import sqlite3

import pytest


def _store(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    conn.execute("CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, filename TEXT)")
    conn.execute("CREATE TABLE q (id INTEGER PRIMARY KEY, memory_id TEXT, status TEXT)")  # status already there
    conn.commit()
    return conn


def _indexes(conn):
    return {r[1] for r in conn.execute("PRAGMA index_list(q)")}


def test_the_other_statements_run_when_the_column_already_exists(tmp_path):
    import migrate_memory

    up = tmp_path / "900_status.up.sql"
    up.write_text(
        "-- adds status; the index must survive a duplicate column\n"
        "ALTER TABLE q ADD COLUMN status TEXT;\n"
        "CREATE INDEX IF NOT EXISTS idx_q_done ON q(memory_id) WHERE status = 'done';\n",
        encoding="utf-8",
    )
    conn = _store(tmp_path)
    migrate_memory.apply_migration(conn, 900, "status", str(up))

    assert "idx_q_done" in _indexes(conn), "the index was dropped with the rolled-back ALTER"
    assert conn.execute("SELECT version FROM schema_versions").fetchall() == [(900,)]


def test_a_real_error_still_applies_nothing(tmp_path):
    import migrate_memory

    up = tmp_path / "901_bad.up.sql"
    up.write_text(
        "ALTER TABLE q ADD COLUMN status TEXT;\n"
        "CREATE INDEX idx_q_x ON q(memory_id);\n"
        "CREATE INDEX idx_bad ON no_such_table(x);\n",
        encoding="utf-8",
    )
    conn = _store(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        migrate_memory.apply_migration(conn, 901, "bad", str(up))

    assert "idx_q_x" not in _indexes(conn), "a failing file must leave nothing applied"
    assert conn.execute("SELECT COUNT(*) FROM schema_versions").fetchone()[0] == 0


def test_statements_split_keeps_a_trigger_body_whole():
    import migrate_memory

    sql = (
        "CREATE TABLE t (a);\n"
        "CREATE TRIGGER tr AFTER INSERT ON t BEGIN\n  UPDATE t SET a = 1;\n  UPDATE t SET a = 2;\nEND;\n"
        "-- trailing comment only\n"
    )
    stmts = migrate_memory._sql_statements(sql)
    assert len(stmts) == 2 and "UPDATE t SET a = 2" in stmts[1]
