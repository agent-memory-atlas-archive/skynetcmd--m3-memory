"""Several processes opening a brand-new SQLite store at once all start.

Switching into WAL needs an exclusive lock, and SQLite refuses it immediately
("database is locked") instead of waiting, so the second m3 process to open a
fresh store crashed during connection setup (found 2026-10-13 by the concurrent
writer benchmark). apply_pragmas now waits within the busy-timeout budget.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
# Like m3's SQLite pool (M3Context._init_sqlite_pool): several connections per
# process, each set up with apply_pragmas. One connection per process does not
# reproduce the crash; the pool does (measured: old code failed 3/3).
_CHILD = (
    "import sqlite3, sys; sys.path.insert(0, 'bin'); from sqlite_pragmas import apply_pragmas; "
    "pool = [sqlite3.connect(sys.argv[1], timeout=30, check_same_thread=False) for _ in range(5)]; "
    "[apply_pragmas(c) for c in pool]; c = pool[0]; "
    "c.execute('CREATE TABLE IF NOT EXISTS t (x)'); c.execute('INSERT INTO t VALUES (1)'); c.commit(); print('ok')"
)


def test_many_processes_can_open_a_fresh_store_together(tmp_path):
    db = tmp_path / "fresh.db"
    sqlite3.connect(db).close()  # a new file in the default (rollback) journal mode
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(db)],
            cwd=_REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(16)
    ]
    results = [p.communicate(timeout=120) for p in procs]
    failed = [err[-500:] for (out, err), p in zip(results, procs, strict=True) if p.returncode != 0]
    assert not failed, failed
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 16


def test_the_wal_switch_retries_a_locked_store_then_reports_the_cause(monkeypatch):
    import sqlite_pragmas as sp

    class _Conn:
        def __init__(self, fail_times):
            self.left = fail_times

        def execute(self, stmt):
            if self.left:
                self.left -= 1
                raise sqlite3.OperationalError("database is locked")

    sp._set_wal(_Conn(3), "PRAGMA journal_mode = WAL")  # succeeds after three refusals
    monkeypatch.setattr(sp, "_BUSY_TIMEOUT_MS", 0)
    with pytest.raises(sqlite3.OperationalError, match="could not switch the store to WAL"):
        sp._set_wal(_Conn(10**6), "PRAGMA journal_mode = WAL")
