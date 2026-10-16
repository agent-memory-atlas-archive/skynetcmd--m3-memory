"""A push watermark advances only after the warehouse COMMIT succeeds.

pg_sync wrote each push watermark to the local store and committed it before
pg_conn.commit(). A failed commit, a step rolled back to its savepoint, or a
failed secrets statement then left the local mark advanced over rows the
warehouse never kept: the next run asked only for newer rows, so those rows were
never sent again.

Every test here FORCES the failure (commit raises, a statement after the push
raises, the watermark write raises) and then runs again cleanly to show the row
is re-pushed — no row is lost.

Hermetic: the local store is a temp SQLite file built through the real migration
chain; the warehouse is a fake connection. No DSN is read.
"""

from __future__ import annotations

import contextlib
import pathlib
import sqlite3
import sys
import uuid
from datetime import UTC, datetime

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "memory" / "migrations"
sys.path.insert(0, str(_BIN))

import pg_sync  # noqa: E402


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split())
        self.conn.statements.append(self._last)
        for trigger in self.conn.fail_on:
            if trigger in self._last:
                raise RuntimeError(f"injected failure on: {trigger}")

    def fetchall(self):
        return []

    def fetchone(self):
        return (datetime.now(UTC),) if "NOW()" in self._last else None


class FakeWarehouseConn:
    def __init__(self, *, fail_commit=False, fail_on=()):
        self.autocommit = True
        self.fail_commit = fail_commit
        self.fail_on = list(fail_on)
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("injected warehouse commit failure")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture()
def store(tmp_path):
    import migrate_memory as m

    db = tmp_path / "agent_memory.db"
    conn = sqlite3.connect(db)
    m.init_migrations_table(conn)
    migs = m.discover_migrations(str(_MIGRATIONS))
    for v in sorted(migs):
        m.apply_migration(conn, v, migs[v]["name"], migs[v]["up"])
    now = datetime.now(UTC).isoformat()
    mem_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO memory_items (id, type, content, created_at, updated_at) VALUES (?, 'note', 'x', ?, ?)",
        (mem_id, now, now),
    )
    conn.commit()
    conn.close()
    return db, mem_id


@pytest.fixture()
def run(store, monkeypatch):
    """run(warehouse) -> (exit code, ids pushed to memory_items)."""
    import psycopg2.extras

    db, _ = store
    pushed: list[str] = []

    def _execute_values(cur, sql, rows, *a, **k):
        if "INSERT INTO memory_items" in sql:
            pushed.extend(r[0] for r in rows)

    monkeypatch.setattr(psycopg2.extras, "execute_values", _execute_values)
    monkeypatch.setattr(pg_sync, "resolve_sync_targets", lambda uri: [pg_sync.SyncTarget("main", str(db), {})])
    monkeypatch.setattr(pg_sync, "_prepare_warehouse", lambda conn: "m3_warehouse")

    def _run(warehouse):
        class _Ctx:
            @contextlib.contextmanager
            def pg_connection(self):
                yield warehouse

        monkeypatch.setattr(pg_sync, "_ctx", lambda: _Ctx())
        monkeypatch.setattr(sys, "argv", ["pg_sync.py", "--db", str(db)])
        pushed.clear()
        rc = pg_sync.main()
        return rc, list(pushed)

    yield _run
    pg_sync._FAILED_DIRECTIONS.clear()
    pg_sync._PENDING_PUSH_WATERMARKS.clear()


def _wm(db, direction):
    with contextlib.closing(sqlite3.connect(db)) as c:
        row = c.execute("SELECT last_synced_at FROM sync_watermarks WHERE direction = ?", (direction,)).fetchone()
    return row[0] if row else None


def _lock_rows(db):
    with contextlib.closing(sqlite3.connect(db)) as c:
        return c.execute("SELECT COUNT(*) FROM sync_locks").fetchone()[0]


def test_failed_warehouse_commit_keeps_push_watermarks_and_the_row_is_resent(store, run):
    db, mem_id = store
    wh = FakeWarehouseConn(fail_commit=True)
    rc, pushed = run(wh)

    assert pushed == [mem_id], "precondition: the row was sent before the commit failed"
    assert rc == 1
    assert "main/target" in pg_sync._FAILED_DIRECTIONS
    for direction in ("pg_push", "rel_push", "emb_push", "tasks_push"):
        assert _wm(db, direction) is None, f"observed: {direction} advanced although the commit failed"
    assert pg_sync._PENDING_PUSH_WATERMARKS == []
    assert wh.rollbacks >= 1, "the failed target's warehouse work must be rolled back"
    assert _lock_rows(db) == 0, "the sync lock must be released on a failed run too"

    rc, pushed = run(FakeWarehouseConn())
    assert rc is None
    assert pushed == [mem_id], "observed: the row whose commit failed was never sent again"
    assert _wm(db, "pg_push") is not None


def test_failure_after_the_push_rolls_back_the_step_and_keeps_its_watermark(store, run):
    db, mem_id = store
    # The memory_items PULL read runs after its push succeeded, in the same step.
    wh = FakeWarehouseConn(fail_on=["FROM memory_items WHERE TRUE"])
    rc, pushed = run(wh)

    assert pushed == [mem_id]
    assert rc == 1
    assert "main/memory_items" in pg_sync._FAILED_DIRECTIONS
    assert "ROLLBACK TO SAVEPOINT items" in wh.statements
    assert _wm(db, "pg_push") is None, "observed: the push mark advanced over a rolled-back step"
    # Scope: the other steps committed, so their marks did advance.
    assert _wm(db, "tasks_push") is not None
    assert _lock_rows(db) == 0

    rc, pushed = run(FakeWarehouseConn())
    assert rc is None and pushed == [mem_id]
    assert _wm(db, "pg_push") is not None


def test_secrets_failure_is_isolated_recorded_and_fails_the_run(store, run):
    db, _ = store
    wh = FakeWarehouseConn(fail_on=["SELECT service_name, encrypted_value"])
    rc, _pushed = run(wh)

    assert rc == 1, "a failed secrets step must not report success"
    assert "main/synchronized_secrets" in pg_sync._FAILED_DIRECTIONS
    assert "SAVEPOINT secrets" in wh.statements and "ROLLBACK TO SAVEPOINT secrets" in wh.statements
    assert wh.commits == 1, "the steps before secrets are still committed"
    assert _wm(db, "pg_push") is not None


def test_watermark_write_failure_after_commit_resends_rather_than_loses(store, run, monkeypatch):
    db, mem_id = store
    real = pg_sync._set_watermark

    def _boom(cur, direction, ts, target):
        if direction.endswith("_push"):
            raise sqlite3.OperationalError("database is locked")
        return real(cur, direction, ts, target)

    monkeypatch.setattr(pg_sync, "_set_watermark", _boom)
    rc, _ = run(FakeWarehouseConn())
    assert rc == 1 and "main/target" in pg_sync._FAILED_DIRECTIONS
    assert _wm(db, "pg_push") is None
    assert _lock_rows(db) == 0

    monkeypatch.setattr(pg_sync, "_set_watermark", real)
    rc, pushed = run(FakeWarehouseConn())
    assert rc is None and pushed == [mem_id], "an idempotent re-push is the safe direction"


def test_run_step_discards_only_the_failed_steps_marks():
    pg_sync._FAILED_DIRECTIONS.clear()
    pg_sync._PENDING_PUSH_WATERMARKS[:] = [("main", "earlier_push", "t0")]
    wh = FakeWarehouseConn()

    def _step():
        pg_sync._defer_push_watermark("x_push", "t1", "main")
        raise RuntimeError("after the push")

    try:
        assert pg_sync._run_step(wh.cursor(), "sp", "main", "x", _step) is False
        assert pg_sync._PENDING_PUSH_WATERMARKS == [("main", "earlier_push", "t0")]
        assert pg_sync._FAILED_DIRECTIONS == ["main/x"]
        assert wh.statements == ["SAVEPOINT sp", "ROLLBACK TO SAVEPOINT sp"]
    finally:
        pg_sync._FAILED_DIRECTIONS.clear()
        pg_sync._PENDING_PUSH_WATERMARKS.clear()
