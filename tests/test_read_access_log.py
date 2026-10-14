"""The read-access audit log records who read which memory, and when (GDPR Art. 30).

Always on, 90-day retention. Rows are queued by the read
paths (memory_get, search hits) with the caller bound by the dispatcher, flushed in
batches, erased by gdpr_forget, exported by gdpr_export, pruned by maintenance.
"""

from __future__ import annotations

import json
import uuid

import pytest

from conftest import gdpr_erase


def _p():
    from memory.backends import dialect

    return dialect().param()


@pytest.fixture
def mem(each_backend):
    """One memory owned by a fresh data subject; cleaned up after."""
    mid, user = str(uuid.uuid4()), f"subject-{uuid.uuid4().hex[:8]}"
    p = _p()
    with each_backend.connection() as conn:
        conn.execute(
            f"INSERT INTO memory_items (id, type, title, content, user_id, is_deleted) "
            f"VALUES ({p}, 'note', 'audit probe', 'audit probe body', {p}, 0)",
            (mid, user),
        )
        conn.commit()
    yield each_backend, mid, user
    with each_backend.connection() as conn:
        conn.execute(f"DELETE FROM memory_access_log WHERE memory_id = {p}", (mid,))
        conn.execute(f"DELETE FROM memory_items WHERE id = {p}", (mid,))
        conn.commit()


def _log(backend, mid):
    p = _p()
    with backend.connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT memory_id, agent_id, user_id, op FROM memory_access_log WHERE memory_id = {p}", (mid,)
            ).fetchall()
        ]


def test_a_get_is_recorded_with_its_caller_and_subject(mem):
    import memory_core
    from m3_core.caller import as_caller
    from memory.db import flush_read_log

    backend, mid, user = mem
    with as_caller("agent-alpha"):
        out = memory_core.memory_get_impl(mid)
    assert mid in out
    flush_read_log()
    assert _log(backend, mid) == [{"memory_id": mid, "agent_id": "agent-alpha", "user_id": user, "op": "get"}]


def test_search_hits_are_recorded_and_a_burst_coalesces(mem):
    from m3_core.caller import as_caller
    from memory.db import flush_read_log
    from memory.search import _stamp_retrieved

    backend, mid, user = mem
    hit = (0.9, {"id": mid, "user_id": user})
    with as_caller("agent-beta"):
        for _ in range(3):  # the same agent reading the same memory in one burst
            _stamp_retrieved([hit])
    flush_read_log()
    assert _log(backend, mid) == [{"memory_id": mid, "agent_id": "agent-beta", "user_id": user, "op": "search_hit"}]


def test_a_read_outside_any_dispatch_is_attributed_to_direct(mem):
    import memory_core
    from memory.db import flush_read_log

    backend, mid, _user = mem
    memory_core.memory_get_impl(mid)
    flush_read_log()
    assert [r["agent_id"] for r in _log(backend, mid)] == ["direct"]


def test_switched_off_it_records_nothing(mem, m3_settings):
    import memory_core
    from memory.db import flush_read_log

    backend, mid, _user = mem
    m3_settings({"audit.read_log": False})
    memory_core.memory_get_impl(mid)
    flush_read_log()
    assert _log(backend, mid) == []


def test_gdpr_export_includes_and_gdpr_forget_erases_the_subjects_rows(mem):
    import memory_core
    from memory.db import flush_read_log
    from memory_maintenance import gdpr_export_impl

    backend, mid, user = mem
    memory_core.memory_get_impl(mid)
    flush_read_log()
    exported = json.loads(gdpr_export_impl(user))
    assert [a["memory_id"] for a in exported["access_log"]] == [mid]
    gdpr_erase(user_id=user)
    assert _log(backend, mid) == [], "gdpr_forget left the subject's read log behind"


def test_maintenance_prunes_rows_past_retention(mem):
    from memory.backends import dialect
    from memory_maintenance import memory_maintenance_impl

    backend, mid, user = mem
    d, p = dialect(), _p()
    with backend.connection() as conn:
        conn.execute(
            f"INSERT INTO memory_access_log (id, memory_id, agent_id, user_id, op, ts) "
            f"VALUES ({p}, {p}, 'old-agent', {p}, 'get', {d.now_minus_days(p)})",
            (uuid.uuid4().hex, mid, user, 400),
        )
        conn.execute(
            f"INSERT INTO memory_access_log (id, memory_id, agent_id, user_id, op, ts) "
            f"VALUES ({p}, {p}, 'new-agent', {p}, 'get', {d.now()})",
            (uuid.uuid4().hex, mid, user),
        )
        conn.commit()
    memory_maintenance_impl(decay=False, purge_expired=False, reinforce=False, prune_orphan_queues=False)
    assert [r["agent_id"] for r in _log(backend, mid)] == ["new-agent"]


def test_the_flusher_does_not_sleep_on_the_shared_time_module(monkeypatch):
    """Code and tests patch time.sleep process-wide (test_m3_halt stubs it to
    fail): a flusher sleeping on it died in the gate, 2026-10-11. It must wait on
    its own Event."""
    import importlib
    import time

    db = importlib.import_module("memory.db")

    class _Stop(BaseException):
        pass

    class _Tick:
        calls = 0

        def wait(self, _timeout):
            self.calls += 1
            if self.calls > 1:
                raise _Stop

    monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("the flusher slept on the shared time module"))
    monkeypatch.setattr(db, "_read_log_tick", _Tick(), raising=False)  # absent before the fix
    monkeypatch.setattr(db, "flush_read_log", lambda: 0)
    with pytest.raises(_Stop):
        db._read_log_loop()


def test_a_superseded_flusher_ends(monkeypatch):
    """A rebuilt memory package starts its own flusher; the old one must stop once
    its module is not the live one and its queue is empty."""
    import importlib

    db = importlib.import_module("memory.db")

    class _Tick:
        def wait(self, _timeout):
            return None

    monkeypatch.setattr(db, "_read_log_tick", _Tick())
    monkeypatch.setattr(db, "flush_read_log", lambda: 0)
    monkeypatch.setattr(db, "_read_log_pending", [])
    monkeypatch.setattr(db, "_THIS_MODULE", object())  # as if a newer module took over
    db._read_log_loop()  # returns instead of looping for ever


def test_a_store_without_the_table_is_reported_once_not_retried(tmp_path, monkeypatch, caplog):
    """A store that predates migration 053 can never take the rows: report once
    with the fix and drop them, instead of an error every 0.25 s for ever."""
    import importlib
    import sqlite3

    db = importlib.import_module("memory.db")
    store = tmp_path / "old.db"
    sqlite3.connect(store).close()  # no memory_access_log table
    monkeypatch.setattr(db, "_lazy_init", lambda *a, **k: None)  # as when migrations are skipped or failed
    monkeypatch.setattr(db, "_read_log_unmigrated", set())
    monkeypatch.setattr(db, "_read_log_pending", [(str(store), "m1", "agent", None, "get")])
    with caplog.at_level("ERROR"):
        db.flush_read_log()
        db._read_log_pending.append((str(store), "m2", "agent", None, "get"))
        db.flush_read_log()
    msgs = [r.getMessage() for r in caplog.records if "memory_access_log" in r.getMessage()]
    assert len(msgs) == 1 and "migration 053" in msgs[0]
    assert db._read_log_pending == [], "rows for an unmigrated store must not be retried for ever"
