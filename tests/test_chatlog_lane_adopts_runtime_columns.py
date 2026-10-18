"""Chatlog migrations 008/009 carry forward a store the runtime fallback touched.

Until 2026-10-13 m3_entities added entity_extraction_queue.status to chatlog stores
at runtime. Such a store already has the column when 008 runs: 008's ALTER hits
"duplicate column", the runner rolls the file back and marks it applied, and 009
(kept separate for exactly this) still creates the index.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


def _up(chatlog: Path, tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "bin" / "migrate_memory.py"
    r = subprocess.run(
        [sys.executable, str(script), "--chatlog-db", str(chatlog), "up", "--yes", "--target", "chatlog"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr[-800:]


def test_a_store_with_the_runtime_column_upgrades_and_gets_the_index(tmp_path):
    chatlog = tmp_path / "agent_chatlog.db"
    _up(chatlog, tmp_path)
    # Back to "before 008", as the runtime fallback left it: column present,
    # index absent, 008/009 not recorded.
    with sqlite3.connect(str(chatlog)) as c:
        c.execute("DROP INDEX IF EXISTS idx_eeq_status_done")
        c.execute("DELETE FROM schema_versions WHERE version IN (8, 9)")

    _up(chatlog, tmp_path)  # the upgrade

    with sqlite3.connect(str(chatlog)) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(entity_extraction_queue)")]
        idx = {r[1] for r in c.execute("PRAGMA index_list(entity_extraction_queue)")}
        versions = {r[0] for r in c.execute("SELECT version FROM schema_versions")}
    assert cols.count("status") == 1
    assert "idx_eeq_status_done" in idx, "009 must create the index even when 008 was already-applied"
    assert {8, 9} <= versions
