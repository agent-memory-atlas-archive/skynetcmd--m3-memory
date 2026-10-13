"""Tests for the pinned/protected memory flag (Lever 3a).

`memory_items.pinned` comes from migration 037 (PostgreSQL: the base schema);
m3 schemas change only through migrations. Pinned memories are exempt from:
  - importance decay (memory_maintenance_impl's `decay` block)
  - confidence decay-toward-neutral (_reinforce_confidence)
  - expiry purge (memory_maintenance_impl's `purge_expired` block)
  - retention TTL / max-count purge (_enforce_retention_policies)

Every pinned-aware query must tolerate the column being absent on an older
DB (COALESCE(pinned,0)=0 wrapped in a try/except that falls back to the
original, pre-pinned query on "no such column: pinned").
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid

import pytest
from m3_core.paths import pin_database

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")


def _full_db(db_path):
    from conftest import create_full_main_schema

    create_full_main_schema(db_path)


def _seed(
    conn, mid, *, importance=0.9, confidence=0.9, created_days_ago=30, expires_at=None, pinned=None, agent_id="agent1"
):
    cols = [
        "id",
        "type",
        "title",
        "content",
        "source",
        "change_agent",
        "created_at",
        "importance",
        "confidence",
        "is_deleted",
        "agent_id",
    ]
    vals = [
        mid,
        "fact",
        "t",
        "c",
        "agent",
        "claude",
        f"datetime('now', '-{created_days_ago} days')",
        importance,
        confidence,
        0,
        agent_id,
    ]
    if expires_at is not None:
        cols.append("expires_at")
        vals.append(expires_at)
    if pinned is not None:
        cols.append("pinned")
        vals.append(pinned)
    placeholders = []
    params = []
    for c, v in zip(cols, vals, strict=True):
        if c == "created_at":
            placeholders.append(v)  # raw SQL expr, not a param
        else:
            placeholders.append("?")
            params.append(v)
    sql = f"INSERT INTO memory_items ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
    conn.execute(sql, params)


# ── the pinned column ─────────────────────────────────────────────────────


def test_the_migrated_schema_has_the_pinned_column(tmp_path):
    """Migration 037 owns the column (m3 schemas change only through migrations);
    there is no runtime backstop to fall back on."""
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
    assert "pinned" in cols


# ── decay exemption ──────────────────────────────────────────────────────────


def test_pinned_row_survives_importance_decay_unpinned_decays(tmp_path):
    from memory_maintenance import memory_maintenance_impl

    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _seed(conn, "pinned-id", importance=0.9, created_days_ago=30, pinned=1)
    _seed(conn, "unpinned-id", importance=0.9, created_days_ago=30, pinned=0)
    conn.commit()
    conn.close()

    pin_database(str(db))
    try:
        memory_maintenance_impl(decay=True, purge_expired=False, prune_orphan_embeddings=False, reinforce=False)
    finally:
        pin_database(None)

    conn = sqlite3.connect(str(db))
    pinned_importance = conn.execute("SELECT importance FROM memory_items WHERE id='pinned-id'").fetchone()[0]
    unpinned_importance = conn.execute("SELECT importance FROM memory_items WHERE id='unpinned-id'").fetchone()[0]
    conn.close()

    assert pinned_importance == pytest.approx(0.9)  # untouched
    assert unpinned_importance < 0.9  # decayed


def test_pinned_row_survives_confidence_decay(tmp_path):
    from memory_maintenance import _reinforce_confidence

    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _seed(conn, "pinned-id", confidence=0.95, pinned=1)
    _seed(conn, "unpinned-id", confidence=0.95, pinned=0)
    conn.commit()

    _reinforce_confidence(conn)
    conn.commit()

    pinned_conf = conn.execute("SELECT confidence FROM memory_items WHERE id='pinned-id'").fetchone()[0]
    unpinned_conf = conn.execute("SELECT confidence FROM memory_items WHERE id='unpinned-id'").fetchone()[0]
    conn.close()

    assert pinned_conf == pytest.approx(0.95)  # untouched
    assert unpinned_conf < 0.95  # decayed toward neutral


# ── expiry exemption ─────────────────────────────────────────────────────────


def test_pinned_row_not_expiry_purged_unpinned_is(tmp_path):
    from memory_maintenance import memory_maintenance_impl

    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    past = "2020-01-01T00:00:00Z"
    _seed(conn, "pinned-id", expires_at=past, pinned=1)
    _seed(conn, "unpinned-id", expires_at=past, pinned=0)
    conn.commit()
    conn.close()

    pin_database(str(db))
    try:
        memory_maintenance_impl(decay=False, purge_expired=True, prune_orphan_embeddings=False, reinforce=False)
    finally:
        pin_database(None)

    conn = sqlite3.connect(str(db))
    remaining_ids = {r[0] for r in conn.execute("SELECT id FROM memory_items WHERE is_deleted = 0")}
    unpinned = conn.execute("SELECT is_deleted FROM memory_items WHERE id = 'unpinned-id'").fetchone()
    conn.close()

    assert "pinned-id" in remaining_ids  # pinned: exempt from expiry
    assert "unpinned-id" not in remaining_ids  # expired: soft-deleted
    assert unpinned == (1,), f"observed {unpinned!r}: expiry must soft-delete (the row stays); hard delete is GDPR-only"


# ── old-DB tolerance (pinned column absent) ──────────────────────────────────


def test_decay_and_expiry_tolerate_missing_pinned_column(tmp_path):
    """A pre-037 DB (no `pinned` column, ensure never run). The pinned-aware
    maintenance queries must fall back to the original query and not crash.
    Simulate the pre-migration state by dropping the column migration 037 adds."""
    from memory_maintenance import memory_maintenance_impl

    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("DROP INDEX IF EXISTS idx_memory_items_pinned")  # index refs the col
    conn.execute("ALTER TABLE memory_items DROP COLUMN pinned")  # → pre-037 state
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
    assert "pinned" not in cols

    past = "2020-01-01T00:00:00Z"
    _seed(conn, "old-schema-id", importance=0.9, expires_at=past)
    conn.commit()
    conn.close()

    pin_database(str(db))
    try:
        # Must not raise even though `pinned` doesn't exist.
        memory_maintenance_impl(decay=True, purge_expired=True, prune_orphan_embeddings=False, reinforce=False)
    finally:
        pin_database(None)

    conn = sqlite3.connect(str(db))
    remaining_ids = {r[0] for r in conn.execute("SELECT id FROM memory_items WHERE is_deleted = 0")}
    conn.close()
    # Fallback path ran the original (non-pinned) query: expired row soft-deleted.
    assert "old-schema-id" not in remaining_ids


def test_retention_ttl_tolerates_missing_pinned_column(tmp_path):
    from memory_maintenance import _enforce_retention_policies

    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _seed(conn, "old-schema-id", created_days_ago=999, agent_id="agentX")
    conn.execute(
        "INSERT INTO agent_retention_policies (agent_id, max_memories, ttl_days, auto_archive) VALUES (?, 1000, 1, 0)",
        ("agentX",),
    )
    conn.commit()

    purged = _enforce_retention_policies(conn)  # must not raise
    conn.commit()

    is_deleted = conn.execute("SELECT is_deleted FROM memory_items WHERE id='old-schema-id'").fetchone()[0]
    conn.close()
    assert purged == 1
    assert is_deleted == 1  # TTL enforcement soft-deletes


# ── retention exemption ──────────────────────────────────────────────────────


def test_pinned_row_survives_retention_ttl_unpinned_purged(tmp_path):
    from memory_maintenance import _enforce_retention_policies

    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _seed(conn, "pinned-id", created_days_ago=999, pinned=1, agent_id="agentX")
    _seed(conn, "unpinned-id", created_days_ago=999, pinned=0, agent_id="agentX")
    conn.execute(
        "INSERT INTO agent_retention_policies (agent_id, max_memories, ttl_days, auto_archive) VALUES (?, 1000, 1, 0)",
        ("agentX",),
    )
    conn.commit()

    _enforce_retention_policies(conn)
    conn.commit()

    pinned_deleted = conn.execute("SELECT is_deleted FROM memory_items WHERE id='pinned-id'").fetchone()[0]
    unpinned_deleted = conn.execute("SELECT is_deleted FROM memory_items WHERE id='unpinned-id'").fetchone()[0]
    conn.close()
    assert pinned_deleted == 0  # exempt — survives TTL enforcement
    assert unpinned_deleted == 1  # soft-deleted by TTL enforcement


def test_pinned_row_survives_retention_max_count_unpinned_purged(tmp_path):
    from memory_maintenance import _enforce_retention_policies

    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    # Oldest row is pinned; max_memories=1 would normally purge it as the
    # "excess" (offset past the newest 1) — pinned exemption must save it.
    _seed(conn, "old-pinned", created_days_ago=10, pinned=1, agent_id="agentY")
    _seed(conn, "newest", created_days_ago=1, pinned=0, agent_id="agentY")
    conn.execute(
        "INSERT INTO agent_retention_policies (agent_id, max_memories, ttl_days, auto_archive) VALUES (?, 1, 0, 0)",
        ("agentY",),
    )
    conn.commit()

    _enforce_retention_policies(conn)
    conn.commit()

    old_pinned_deleted = conn.execute("SELECT is_deleted FROM memory_items WHERE id='old-pinned'").fetchone()[0]
    newest_deleted = conn.execute("SELECT is_deleted FROM memory_items WHERE id='newest'").fetchone()[0]
    conn.close()
    assert old_pinned_deleted == 0  # exempt despite being the excess row
    assert newest_deleted == 0


# ── memory_pin_impl / memory_unpin_impl ──────────────────────────────────────


def test_memory_pin_and_unpin_impl(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    mid = str(uuid.uuid4())
    _seed(conn, mid)
    conn.commit()
    conn.close()

    pin_database(str(db))
    import memory_core

    result = memory_core.memory_pin_impl(mid)
    assert result == f"Pinned: {mid}"

    conn = sqlite3.connect(str(db))
    val = conn.execute("SELECT pinned FROM memory_items WHERE id=?", (mid,)).fetchone()[0]
    assert val == 1

    result = memory_core.memory_unpin_impl(mid)
    assert result == f"Unpinned: {mid}"
    val = conn.execute("SELECT pinned FROM memory_items WHERE id=?", (mid,)).fetchone()[0]
    conn.close()
    assert val == 0


def test_memory_pin_impl_accepts_8char_prefix(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    mid = str(uuid.uuid4())
    _seed(conn, mid)
    conn.commit()
    conn.close()

    pin_database(str(db))
    import memory_core

    result = memory_core.memory_pin_impl(mid[:8])
    assert result == f"Pinned: {mid}"


def test_memory_pin_impl_not_found(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _full_db(db)
    pin_database(str(db))
    import memory_core

    result = memory_core.memory_pin_impl(str(uuid.uuid4()))
    assert result.startswith("Error:")


# ── pinned ≠ supersede-proof (the "pinned but contradicted" boundary) ─────────


def test_pinned_row_can_still_be_superseded(tmp_path, monkeypatch):
    """Pin protects against lifecycle *aging* (decay/expiry/retention) but NOT
    against *correction*: a pinned memory can still be explicitly superseded when
    new contradictory truth arrives. This locks the intended boundary — the pin
    flag means "don't let this rot," not "never update this."

    Confirmed intent: the module docstring and CHANGELOG scope the pinned
    exemption to decay/confidence-decay/expiry/retention only; the supersede path
    (write.py::memory_supersede_impl) carries no `pinned` check by design. This
    test asserts that design and guards it against silent regression.
    """
    import asyncio

    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    old_id = str(uuid.uuid4())
    _seed(conn, old_id, importance=0.9, pinned=1)
    conn.commit()
    conn.close()

    pin_database(str(db))
    import memory_core

    result = asyncio.run(memory_core.memory_supersede_impl(old_id, content="corrected fact", embed=False))
    assert result.startswith("Superseded"), result

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    old_row = conn.execute("SELECT pinned, is_deleted FROM memory_items WHERE id=?", (old_id,)).fetchone()
    # A `supersedes` edge new -> old was written despite the old row being pinned.
    edge = conn.execute(
        "SELECT COUNT(*) FROM memory_relationships WHERE to_id=? AND relationship_type='supersedes'",
        (old_id,),
    ).fetchone()[0]
    conn.close()

    # The pinned old row was superseded (closed) — pin did not block correction.
    assert old_row["is_deleted"] == 1
    # Still flagged pinned (supersede doesn't touch the flag; it closes the row).
    assert old_row["pinned"] == 1
    assert edge == 1
