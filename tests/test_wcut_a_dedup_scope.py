"""memory_dedup pairs duplicates only within one (user, agent, embed model)
partition, never offers a pinned memory, and applies through the supersede
write -- reporting the rows it actually changed."""

from __future__ import annotations

import importlib
import uuid

import pytest


def _p():
    from memory.backends import dialect

    return dialect().param()


@pytest.fixture
def store(each_backend):
    mc = importlib.import_module("memory_core")
    tag = uuid.uuid4().hex[:8]
    vec = mc._pack([0.6, 0.8, 0.0, 0.0])
    other = mc._pack([0.0, 0.0, 1.0, 0.0])
    rows = {
        # id: (user, agent, model, pinned, vector)
        "a1": ("u1", "ag", "m-x", 0, vec),
        "a2": ("u1", "ag", "m-x", 0, vec),  # duplicate of a1
        "b1": ("u2", "ag", "m-x", 0, vec),  # same vector, other subject
        "c1": ("u1", "ag", "m-y", 0, vec),  # same vector, other model
        "p1": ("u1", "ag", "m-x", 1, vec),  # pinned
        "d1": ("u1", "ag", "m-x", 0, other),
    }
    ids = {k: f"{k}-{tag}" for k in rows}
    p = _p()
    with each_backend.connection() as c:
        for k, (user, agent, model, pinned, v) in rows.items():
            c.execute(
                f"INSERT INTO memory_items (id, type, title, content, user_id, agent_id, pinned, is_deleted) "
                f"VALUES ({p}, 'note', {p}, {p}, {p}, {p}, {p}, 0)",
                (ids[k], k, k, f"{user}-{tag}", f"{agent}-{tag}", pinned),
            )
            c.execute(
                f"INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, vector_kind) "
                f"VALUES ({p}, {p}, {p}, {p}, 4, 'default')",
                (str(uuid.uuid4()), ids[k], v, model),
            )
        c.commit()
    yield each_backend, ids
    ph = ", ".join([p] * len(ids))
    vals = list(ids.values())
    with each_backend.connection() as c:
        c.execute(f"DELETE FROM memory_history WHERE memory_id IN ({ph})", vals)
        c.execute(f"DELETE FROM memory_relationships WHERE from_id IN ({ph})", vals)
        c.execute(f"DELETE FROM memory_embeddings WHERE memory_id IN ({ph})", vals)
        c.execute(f"DELETE FROM memory_items WHERE id IN ({ph})", vals)
        c.commit()


def _mine(result, ids):
    mine = set(ids.values())
    return [(g["a"], g["b"]) for g in result["groups"] if g["a"] in mine or g["b"] in mine]


def test_pairs_stay_inside_one_subject_and_model_and_skip_pinned(store):
    _backend, ids = store
    mm = importlib.import_module("memory_maintenance")
    out = mm.memory_dedup_impl(threshold=0.99, dry_run=True)
    pairs = _mine(out, ids)
    assert len(pairs) == 1 and set(pairs[0]) == {ids["a1"], ids["a2"]}, pairs


def test_apply_supersedes_and_counts_rows_changed(store):
    backend, ids = store
    mm = importlib.import_module("memory_maintenance")
    out = mm.memory_dedup_impl(threshold=0.99, dry_run=False)
    pairs = _mine(out, ids)
    assert len(pairs) == 1
    kept, dup = pairs[0]
    assert out["applied_count"] >= 1 and dup not in out["not_applied"]
    p = _p()
    with backend.connection() as c:
        row = c.execute(f"SELECT is_deleted, valid_to, updated_at FROM memory_items WHERE id = {p}", (dup,)).fetchone()
        assert row["is_deleted"] == 1 and row["valid_to"] and row["updated_at"]
        edge = c.execute(
            f"SELECT relationship_type FROM memory_relationships WHERE from_id = {p} AND to_id = {p}", (kept, dup)
        ).fetchone()
        assert edge is not None and edge["relationship_type"] == "supersedes"
        hist = c.execute(f"SELECT event FROM memory_history WHERE memory_id = {p}", (dup,)).fetchall()
        assert any(h["event"] == "supersede" for h in hist)
