"""The entity-coalescing schema reaches every store through migrations.

057 repairs a main store that 043 never fully reached: the runner of that time
rolled 043 back whole on a column a runtime fallback had already added, then
marked it applied, so the two tables and their indexes were never created. 011
gives the chatlog lane the same schema, which the coalescing pass used to add at
runtime.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "migrate_memory.py"


def _up(tmp_path, *args):
    # The child inherits the sandbox's M3_CONFIG_ROOT, whose [paths] keeps every
    # root under tmp; the stores are named with --database / --chatlog-db.
    env = dict(os.environ)
    r = subprocess.run([sys.executable, str(_SCRIPT), *args], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    return r


def _objects(db):
    with sqlite3.connect(str(db)) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        cols = {r[1] for r in c.execute("PRAGMA table_info(entities)")}
    return tables, indexes, cols


def test_057_repairs_a_main_store_whose_043_was_rolled_back(tmp_path):
    db = tmp_path / "agent_memory.db"
    _up(tmp_path, "--database", str(db), "up", "--yes", "--target", "main")
    # The state the old runner left: 043 recorded, its columns present (the runtime
    # fallback added them), its tables and indexes absent; 057 not yet run.
    with sqlite3.connect(str(db)) as c:
        c.execute("DROP TABLE entity_coalesce_candidates")
        c.execute("DROP TABLE entity_coalesce_embeddings")
        c.execute("DROP INDEX idx_entities_coalesce_state")
        c.execute("DROP INDEX idx_entities_cluster")
        c.execute("DELETE FROM schema_versions WHERE version = 57")

    _up(tmp_path, "--database", str(db), "up", "--yes", "--target", "main")  # the upgrade

    tables, indexes, cols = _objects(db)
    assert {"entity_coalesce_candidates", "entity_coalesce_embeddings"} <= tables
    assert {"idx_entities_coalesce_state", "idx_entities_cluster", "idx_ecc_reviewed"} <= indexes
    assert {"coalesce_state", "cluster_id", "resolution_run"} <= cols


def test_a_chatlog_store_gets_the_coalescing_schema_from_its_own_lane(tmp_path):
    chatlog = tmp_path / "agent_chatlog.db"
    _up(tmp_path, "--chatlog-db", str(chatlog), "up", "--yes", "--target", "chatlog")
    tables, indexes, cols = _objects(chatlog)
    assert {"idx_entities_coalesce_state", "idx_entities_cluster", "idx_ecc_reviewed"} <= indexes
    assert {"entity_coalesce_candidates", "entity_coalesce_embeddings"} <= tables
    assert {"coalesce_state", "cluster_id", "resolution_run"} <= cols
