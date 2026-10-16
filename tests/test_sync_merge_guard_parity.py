"""The bridge (pg_sync) and the FDW fast path merge with ONE definition, and the
fast path's watermarks come from server clocks.

The FDW copy of the memory_items guard had drifted to a bare `target < EXCLUDED`:
no NULL arm (a stored row with no timestamp never accepted an update) and no
manual-curation protection (a PG primary overwrote edits the bridge protected).
Its watermarks were this host's clock compared with peers' timestamps, so a row
pushed late by another machine was skipped for good.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import sys
from datetime import UTC, datetime

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

import pg_fdw_sync as F  # noqa: E402

_COLS = ["id", "content", "change_agent", "updated_at"]


def _spec(table):
    return next(s for s in F._TABLE_SPECS if s[0] == table)


class _Rec:
    rowcount = 0

    def __init__(self):
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))


class TestOneDefinition:
    def test_pg_sync_has_no_inline_copy_of_a_merge_guard(self):
        src = (_BIN / "pg_sync.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert "NOT IN ('manual', 'system')" not in code
        assert not re.search(r"updated_at IS NULL OR (EXCLUDED|excluded)\.updated_at", code)
        assert "EXCLUDED.version >" not in code and "excluded.version >" not in code
        for name in ("lww_guard", "version_guard", "arrival_bound"):
            assert name in code

    def test_fdw_memory_items_uses_the_curation_guard(self):
        guard = _spec("memory_items")[4]
        assert guard == F.lww_guard("{target}", protect_curation=True)

    def test_fdw_secrets_uses_the_version_guard(self):
        assert _spec("synchronized_secrets")[4] == F.version_guard("{target}")


@pytest.fixture()
def table():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY, content TEXT, change_agent TEXT, updated_at TEXT)")
    yield c
    c.close()


def _merge(c, row):
    from memory.backends.sqlite_backend import SqliteBackend

    SqliteBackend().bulk_upsert(
        c,
        "memory_items",
        _COLS,
        [row],
        conflict_target="(id)",
        update_columns=_COLS[1:],
        guard_sql=F.lww_guard("memory_items", protect_curation=True),
    )
    return c.execute("SELECT content FROM memory_items WHERE id = ?", (row[0],)).fetchone()[0]


class TestGuardBehaviour:
    """The shared guard's semantics, executed (SQLite; the text is portable)."""

    def test_null_stored_timestamp_accepts(self, table):
        table.execute("INSERT INTO memory_items VALUES ('a', 'old', 'auto', NULL)")
        assert _merge(table, ("a", "new", "auto", "2026-01-01")) == "new"

    def test_newer_wins_older_loses(self, table):
        table.execute("INSERT INTO memory_items VALUES ('a', 'keep', 'auto', '2026-06-01')")
        assert _merge(table, ("a", "older", "auto", "2026-01-01")) == "keep"
        assert _merge(table, ("a", "newer", "auto", "2026-07-01")) == "newer"

    def test_curated_row_yields_only_to_a_manual_edit(self, table):
        table.execute("INSERT INTO memory_items VALUES ('a', 'curated', 'manual', '2026-01-01')")
        assert _merge(table, ("a", "automated", "auto", "2026-09-01")) == "curated"
        assert _merge(table, ("a", "hand-edit", "manual", "2026-09-01")) == "hand-edit"


class _FdwCursor(_Rec):
    def __init__(self, primary_now):
        super().__init__()
        self.primary_now = primary_now

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchone(self):
        last = self.sql[-1]
        if "current_schema()" in last:
            return ("m3",)
        if "NOW()" in last:
            return (self.primary_now,)
        return None


class _FdwConn:
    def __init__(self, cur):
        self.cur, self.commits = cur, 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _run_fdw(monkeypatch, warehouse_bound):
    monkeypatch.setattr(F, "_ensure_fdw_wired", lambda cur, dsn: None)
    primary_now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    cur = _FdwCursor(primary_now)
    wm: dict = {}
    F.sync_pg_to_pg(_FdwConn(cur), "postgresql://u@wh/w", wm.get, wm.__setitem__, warehouse_bound=warehouse_bound)
    return cur, wm, primary_now


class TestFdwWatermarks:
    def test_marks_come_from_server_clocks_not_this_host(self, monkeypatch):
        cur, wm, primary_now = _run_fdw(monkeypatch, lambda: "2026-10-01T11:00:00+00:00")
        assert wm["fdw_memory_items_push"] == primary_now.isoformat()
        assert wm["fdw_memory_items_pull_arrival"] == "2026-10-01T11:00:00+00:00"
        assert wm["fdw_tasks_pull"] == "2026-10-01T11:00:00+00:00"
        assert "fdw_memory_items_pull" not in wm, "the old host-clock key must not be advanced"
        # The primary's tables are addressed in ITS schema, not a hardcoded public.
        assert any('"m3".memory_items' in s for s in cur.sql)
        assert not any("public." in s for s in cur.sql)

    def test_memory_items_pull_reads_by_warehouse_arrival(self, monkeypatch):
        monkeypatch.setattr(F, "_ensure_fdw_wired", lambda cur, dsn: None)
        cur = _FdwCursor(datetime(2026, 10, 1, tzinfo=UTC))
        wm = {"fdw_memory_items_pull_arrival": "2026-09-01T00:00:00+00:00"}
        F.sync_pg_to_pg(_FdwConn(cur), "dsn", wm.get, wm.__setitem__, warehouse_bound=lambda: None)
        pull = next(s for s in cur.sql if s.startswith('INSERT INTO "m3".memory_items'))
        assert "FROM cdw_fdw.memory_items WHERE wh_arrived_at > %s" in pull
        assert "change_agent NOT IN ('manual', 'system')" in pull

    def test_unreadable_warehouse_clock_does_not_advance_pull_marks(self, monkeypatch, caplog):
        def _boom():
            raise RuntimeError("warehouse unreachable")

        with caplog.at_level("WARNING"):
            _cur, wm, _ = _run_fdw(monkeypatch, _boom)
        assert not [k for k in wm if "_pull" in k], wm
        assert "fdw_memory_items_push" in wm, "push marks come from the primary and still advance"
        assert "warehouse unreachable" in caplog.text and "NOT advanced" in caplog.text
