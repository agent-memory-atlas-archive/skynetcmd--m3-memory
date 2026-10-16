"""Retention: TTL expiry is archived (restorable by naming the reason), each
removal stamps updated_at and a history event, and an excess row whose archive
write fails is left live instead of deleted without its tombstone."""

from __future__ import annotations

import importlib
import shutil
import uuid

import pytest
from m3_core.paths import pin_database


@pytest.fixture
def store(main_db_template, tmp_path):
    db_path = tmp_path / "retention.db"
    shutil.copy2(main_db_template, db_path)
    pin_database(str(db_path))
    mc = importlib.import_module("memory_core")
    agent = f"ret-{uuid.uuid4().hex[:6]}"

    def add(age_days, pinned=0):
        mid = str(uuid.uuid4())
        with mc._db() as db:
            db.execute(
                "INSERT INTO memory_items (id, content, title, type, agent_id, pinned, is_deleted, created_at) "
                f"VALUES (?, 'b', 't', 'note', ?, ?, 0, datetime('now', '-{int(age_days)} days'))",
                (mid, agent, pinned),
            )
        return mid

    yield mc, agent, add
    pin_database(None)


def _row(mc, sql, args):
    with mc._db() as db:
        return db.execute(sql, args).fetchone()


def test_ttl_archives_stamps_and_records(store):
    mc, agent, add = store
    mm = importlib.import_module("memory_maintenance")
    old, fresh, pinned = add(40), add(1), add(40, pinned=1)
    with mc._db() as db:
        db.execute(
            "INSERT INTO agent_retention_policies (agent_id, max_memories, ttl_days, auto_archive) VALUES (?, 0, 30, 0)",
            (agent,),
        )
        assert mm._enforce_retention_policies(db) == 1
    r = _row(mc, "SELECT is_deleted, updated_at FROM memory_items WHERE id = ?", (old,))
    assert r["is_deleted"] == 1 and r["updated_at"]
    assert (
        _row(mc, "SELECT archive_reason FROM memory_archive WHERE id = ?", (old,))["archive_reason"] == "retention_ttl"
    )
    assert (
        _row(mc, "SELECT COUNT(*) AS n FROM memory_history WHERE memory_id = ? AND event = 'delete'", (old,))["n"] == 1
    )
    for mid in (fresh, pinned):
        assert _row(mc, "SELECT is_deleted FROM memory_items WHERE id = ?", (mid,))["is_deleted"] == 0
    assert mm.memory_restore_impl(memory_id=old, reason="retention_ttl")["would_restore"] == 1


def test_excess_row_whose_archive_fails_stays_live(store, monkeypatch):
    mc, agent, add = store
    mm = importlib.import_module("memory_maintenance")
    a, _b = add(3), add(2)
    monkeypatch.setattr(mm, "_transfer_to_archive", lambda *a, **k: False)
    with mc._db() as db:
        db.execute(
            "INSERT INTO agent_retention_policies (agent_id, max_memories, ttl_days, auto_archive) VALUES (?, 1, 0, 1)",
            (agent,),
        )
        assert mm._enforce_retention_policies(db) == 0
    assert _row(mc, "SELECT is_deleted FROM memory_items WHERE id = ?", (a,))["is_deleted"] == 0
