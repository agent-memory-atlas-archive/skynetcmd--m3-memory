"""The dispatch store's chain has a runner.

The first-send bootstrap used to execute 001 itself and return whenever the table
existed, so a later migration could never have reached an existing store. It now
asks the runner (``migrate_memory.apply_pending``), and ``migrate_memory --target
dispatch`` migrates the store on demand.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _versions(conn):
    return {r[0] for r in conn.execute("SELECT version FROM schema_versions")}


def test_a_later_migration_reaches_an_existing_store(tmp_path):
    import migrate_memory

    chain = tmp_path / "dispatch_migrations"
    chain.mkdir()
    shutil.copy(ROOT / "memory" / "dispatch_migrations" / "001_bootstrap.up.sql", chain)
    conn = sqlite3.connect(str(tmp_path / "agent_dispatch.db"))
    assert migrate_memory.apply_pending(conn, str(chain)) == [1]

    # A release ships 002; the store already exists and is at 001.
    (chain / "002_probe.up.sql").write_text(
        "ALTER TABLE notification_dispatch ADD COLUMN probe TEXT;\n", encoding="utf-8"
    )
    assert migrate_memory.apply_pending(conn, str(chain)) == [2]
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notification_dispatch)")}
    assert "probe" in cols and _versions(conn) >= {1, 2}
    assert migrate_memory.apply_pending(conn, str(chain)) == []  # idempotent


def test_the_cli_migrates_the_dispatch_store(tmp_path):
    engine = tmp_path / "engine"
    env = dict(os.environ, M3_ENGINE_ROOT=str(engine))
    env.pop("M3_DISPATCH_DB", None)
    r = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "migrate_memory.py"), "up", "--yes", "--target", "dispatch"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr[-800:]
    with sqlite3.connect(str(engine / "agent_dispatch.db")) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"notification_dispatch", "deliberation_log"} <= tables
        assert 1 in _versions(conn)


def test_the_first_send_bootstrap_uses_the_runner(tmp_path, monkeypatch):
    from m3_core import context

    calls = []
    import migrate_memory

    real = migrate_memory.apply_pending
    monkeypatch.setattr(migrate_memory, "apply_pending", lambda conn, d: calls.append(d) or real(conn, d))
    monkeypatch.setattr(context, "_DISPATCH_MIGRATED", set())
    conn = sqlite3.connect(str(tmp_path / "agent_dispatch.db"))
    context._ensure_dispatch_schema(conn)
    context._ensure_dispatch_schema(conn)  # once per store per process
    assert len(calls) == 1 and calls[0].endswith("dispatch_migrations")
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='notification_dispatch'").fetchone()
