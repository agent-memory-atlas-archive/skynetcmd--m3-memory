"""A sync direction that fails does not advance its watermark, and the run fails.

pg_sync used to catch a batch error, log it, and advance the watermark anyway:
the next run asked only for rows newer than the watermark, so a row whose batch
failed was never sent again unless it was edited, and the run exited 0 (the
MacBook's "pull reported success" report, m3 64c868d5). Plan Step 2D.U.

Hermetic: the local store is built through the real migration chain; the
warehouse is a fake cursor, and a failure is injected at the push
(execute_values) or the pull (the local bulk_upsert).
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import uuid
from datetime import UTC, datetime

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "memory" / "migrations"
sys.path.insert(0, str(_BIN))

import pg_sync  # noqa: E402

_COLS = (
    "id, type, title, content, metadata_json, agent_id, model_id, change_agent, importance, source, "
    "origin_device, is_deleted, expires_at, decay_rate, created_at, updated_at, user_id, scope, "
    "valid_from, valid_to, content_hash"
).split(", ")


class FakeWarehouse:
    """Answers the statements sync_memory_items sends to PostgreSQL."""

    def __init__(self, remote_rows=()):
        self.remote_rows = list(remote_rows)
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split())

    def fetchall(self):
        if "information_schema.columns" in self._last:
            return [(c,) for c in _COLS]  # nothing to ALTER
        if self._last.startswith("SELECT id, type"):
            return list(self.remote_rows)
        return []

    def fetchone(self):
        return (datetime.now(UTC),) if "NOW()" in self._last else None


@pytest.fixture()
def local():
    import migrate_memory as m

    db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    m.init_migrations_table(conn)
    migs = m.discover_migrations(str(_MIGRATIONS))
    for v in sorted(migs):
        m.apply_migration(conn, v, migs[v]["name"], migs[v]["up"])
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO memory_items (id, type, content, created_at, updated_at) VALUES (?, 'note', 'x', ?, ?)",
        (str(uuid.uuid4()), now, now),
    )
    conn.commit()
    pg_sync._FAILED_DIRECTIONS.clear()
    yield conn
    pg_sync._FAILED_DIRECTIONS.clear()
    conn.close()


def _wm(conn, direction):
    row = conn.execute("SELECT last_synced_at FROM sync_watermarks WHERE direction = ?", (direction,)).fetchone()
    return row[0] if row else None


def _remote_row():
    now = datetime.now(UTC).isoformat()
    vals: dict = dict.fromkeys(_COLS)
    vals.update(id=str(uuid.uuid4()), type="note", content="remote", metadata_json="{}", is_deleted=0,
                created_at=now, updated_at=now, user_id="", scope="agent", content_hash="")  # fmt: skip
    return tuple(vals[c] for c in _COLS)


def _boom(*_a, **_k):
    raise RuntimeError("injected batch failure")


def test_a_failed_push_keeps_the_push_watermark_and_fails_the_run(local, monkeypatch):
    import psycopg2.extras

    monkeypatch.setattr(psycopg2.extras, "execute_values", _boom)
    pg_sync.sync_memory_items(local.cursor(), FakeWarehouse(), local, "main")

    assert _wm(local, "pg_push") is None, "observed: the push watermark advanced past a failed batch"
    assert _wm(local, "pg_pull") is not None, "a clean pull still advances its own watermark"
    assert pg_sync._FAILED_DIRECTIONS == ["main/pg_push"]
    assert pg_sync._failed_run_exit_code() == 1


def test_a_failed_pull_keeps_the_pull_watermark(local, monkeypatch):
    import psycopg2.extras

    monkeypatch.setattr(psycopg2.extras, "execute_values", lambda *a, **k: None)

    class Broken:
        def bulk_upsert(self, *a, **k):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(pg_sync, "_local_backend", lambda: Broken())
    pg_sync.sync_memory_items(local.cursor(), FakeWarehouse([_remote_row()]), local, "main")

    assert _wm(local, "pg_pull") is None, "observed: the pull watermark advanced past a failed batch"
    assert _wm(local, "pg_push") is not None
    assert pg_sync._FAILED_DIRECTIONS == ["main/pg_pull"]


def test_a_clean_run_advances_both_and_exits_clean(local, monkeypatch):
    import psycopg2.extras

    monkeypatch.setattr(psycopg2.extras, "execute_values", lambda *a, **k: None)
    pg_sync.sync_memory_items(local.cursor(), FakeWarehouse(), local, "main")

    assert _wm(local, "pg_push") is not None and _wm(local, "pg_pull") is not None
    assert pg_sync._FAILED_DIRECTIONS == []
    assert pg_sync._failed_run_exit_code() is None
