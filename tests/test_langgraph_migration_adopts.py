"""Migration 054 adopts the LangGraph tables an install already has.

Before 054 the integration created m3_lg_checkpoints at first use, so an upgrading
user's store holds it in the earlier shape, with their checkpoints in it. The
migration must bring that table forward (add deleted_at) and keep every row: m3
schemas change only through migrations, which carry users with them.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

_OLD_SHAPE = """CREATE TABLE m3_lg_checkpoints (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    parent_id      TEXT,
    user_id        TEXT NOT NULL DEFAULT '',
    type           TEXT,
    metadata_type  TEXT,
    checkpoint     BLOB NOT NULL,
    metadata       BLOB NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
)"""


def _migrate(db: str, tmp_path) -> None:
    """The real migrator, as an install runs it: a child given --database."""
    script = Path(__file__).resolve().parents[1] / "bin" / "migrate_memory.py"
    env = dict(os.environ, M3_ENGINE_ROOT=str(tmp_path / "engine"))  # its backups land here
    r = subprocess.run(
        [sys.executable, str(script), "--database", db, "up", "--yes", "--target", "main"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, f"migrator exit {r.returncode}: {r.stderr[-800:]}"


def _columns(db: str, table: str) -> set[str]:
    with sqlite3.connect(db) as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}


def test_an_existing_old_shape_table_is_brought_forward_with_its_rows(tmp_path):
    # A v053 store whose integration already made the old-shape table, holding a
    # user's checkpoint: migrate fully, then put the store back to "before 054".
    db = str(tmp_path / "upgrading.db")
    _migrate(db, tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("DROP TABLE m3_lg_writes")
        c.execute("DROP TABLE m3_lg_checkpoints")
        c.execute(_OLD_SHAPE)
        c.execute(
            "INSERT INTO m3_lg_checkpoints (thread_id, checkpoint_id, checkpoint, metadata, created_at) "
            "VALUES ('t1', 'c1', x'00', x'00', '2026-10-01T00:00:00+00:00')"
        )
        c.execute("DELETE FROM schema_versions WHERE version = 54")
    assert "deleted_at" not in _columns(db, "m3_lg_checkpoints")

    _migrate(db, tmp_path)  # the upgrade

    assert "deleted_at" in _columns(db, "m3_lg_checkpoints"), "054 did not add deleted_at to the existing table"
    with sqlite3.connect(db) as c:
        rows = c.execute("SELECT thread_id, deleted_at FROM m3_lg_checkpoints").fetchall()
    assert rows == [("t1", None)], f"observed: {rows!r}; the user's checkpoint must survive, not deleted"
    assert "idx" in _columns(db, "m3_lg_writes"), "054 did not create m3_lg_writes"


def test_a_fresh_store_gets_both_tables_in_the_current_shape(tmp_path):
    db = str(tmp_path / "fresh.db")
    _migrate(db, tmp_path)

    assert "deleted_at" in _columns(db, "m3_lg_checkpoints")
    assert "blob" in _columns(db, "m3_lg_writes")
