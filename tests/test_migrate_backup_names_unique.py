"""Pre-migration backups of a same-named store, taken in the same second, get
distinct names. A seconds-only name made the second backup refuse to overwrite the
first and fail its whole migration ("no such table: memory_items" afterwards),
seen on the Windows PostgreSQL lane."""

from __future__ import annotations

import sqlite3

import migrate_memory


def test_two_backups_in_the_same_second_both_succeed(tmp_path):
    db = tmp_path / "agent_memory.db"
    sqlite3.connect(db).close()
    target = migrate_memory.MigrationTarget(name="main", db_path=str(db), migrations_dir=str(tmp_path))
    first = migrate_memory.take_backup(str(tmp_path / "backups"), 0, "pre-up", target)
    second = migrate_memory.take_backup(str(tmp_path / "backups"), 0, "pre-up", target)
    assert first and second and first != second
