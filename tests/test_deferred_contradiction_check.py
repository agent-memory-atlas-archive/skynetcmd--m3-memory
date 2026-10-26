"""A write made while no embedder is reachable gets its contradiction check at backfill.

The write path checks contradictions only when it has a vector. Without one it now
flags the row (metadata_json contradiction_check=deferred) and embed_backfill runs
the check after embedding it, then clears the flag. A missing vector alone is not
the signal: writes that opted out and legacy rows also lack vectors and must not
be checked (they could mass-supersede).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import uuid

import pytest

VEC = [0.6, 0.0, 0.8, 0.0]  # unit length; identical vectors -> cosine 1.0
OTHER = [0.0, 1.0, 0.0, 0.0]  # orthogonal to VEC: never a contradiction candidate


def _p():
    from memory.backends import dialect

    return dialect().param()


def _insert(backend, mid: str, content: str, *, meta: dict | None = None, vec: list[float] | None = None) -> None:
    from embedding_utils import pack

    p = _p()
    with backend.connection() as conn:
        conn.execute(
            f"INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, importance, scope, is_deleted) "
            f"VALUES ({p}, 'fact', 'deploy target', {p}, {p}, 'agent-x', 0.5, 'agent', 0)",
            (mid, content, json.dumps(meta or {})),
        )
        if vec is not None:
            conn.execute(
                f"INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim) VALUES ({p}, {p}, {p}, 't', {p})",
                (str(uuid.uuid4()), mid, pack(vec), len(vec)),
            )
        conn.commit()


def _row(backend, mid: str):
    p = _p()
    with backend.connection() as conn:
        return conn.execute(f"SELECT is_deleted, metadata_json FROM memory_items WHERE id = {p}", (mid,)).fetchone()


def _meta(row) -> dict:
    from memory.backends import dialect

    return dialect().json_column_to_dict(row["metadata_json"])


@pytest.fixture
def ids(each_backend):
    tag = uuid.uuid4().hex[:8]
    made = {k: f"dcc{tag}-{k}" for k in ("old", "new", "optout", "legacy")}
    yield each_backend, made
    p = _p()
    with each_backend.connection() as conn:
        for mid in made.values():
            conn.execute(f"DELETE FROM memory_relationships WHERE from_id = {p} OR to_id = {p}", (mid, mid))
            conn.execute(f"DELETE FROM memory_history WHERE memory_id = {p}", (mid,))
            conn.execute(f"DELETE FROM memory_embeddings WHERE memory_id = {p}", (mid,))
            conn.execute(f"DELETE FROM memory_items WHERE id = {p}", (mid,))
        conn.commit()


def test_an_embedder_down_write_is_flagged_and_an_opted_out_one_is_not(each_backend, monkeypatch):
    import memory_core as mc
    from memory.write import contradiction_check_owed

    monkeypatch.setattr(importlib.import_module("memory.embed"), "fast_embedder_available", lambda: False)
    made = []
    try:
        for check in (True, False):
            r = asyncio.run(
                mc.memory_write_impl(
                    type="fact",
                    content=f"deferred write check={check} {uuid.uuid4().hex}",
                    title="t",
                    check_contradictions=check,
                )
            )
            assert r.startswith("Created:"), r
            mid = r.split("Created:", 1)[1].split()[0]
            made.append(mid)
            owed = contradiction_check_owed(_row(each_backend, mid)["metadata_json"])
            assert owed is check, f"observed: check={check} flagged={owed} on {each_backend.name}"
    finally:
        p = _p()
        with each_backend.connection() as conn:
            for mid in made:
                conn.execute(f"DELETE FROM memory_history WHERE memory_id = {p}", (mid,))
                conn.execute(f"DELETE FROM memory_items WHERE id = {p}", (mid,))
            conn.commit()


def test_a_caller_who_passes_embed_false_is_not_flagged(each_backend):
    """embed=False is the caller choosing no vector: no flag, metadata untouched
    (the PG end-to-end write test caught the first version adding the key)."""
    import memory_core as mc

    r = asyncio.run(
        mc.memory_write_impl(type="fact", content=f"no-embed write {uuid.uuid4().hex}", title="t", embed=False)
    )
    mid = r.split("Created:", 1)[1].split()[0]
    try:
        assert "contradiction_check" not in _meta(_row(each_backend, mid)), f"observed: flagged on {each_backend.name}"
    finally:
        p = _p()
        with each_backend.connection() as conn:
            conn.execute(f"DELETE FROM memory_history WHERE memory_id = {p}", (mid,))
            conn.execute(f"DELETE FROM memory_items WHERE id = {p}", (mid,))
            conn.commit()


def _sweep_args(prefix: str) -> argparse.Namespace:
    from m3_sdk import resolve_db_path

    return argparse.Namespace(
        db=resolve_db_path(None), target="core", variant=[], type=[], user_id=None, scope=None,
        id_prefix=prefix, max_age_days=None, limit=None, batch_size=4, concurrency=1,
        connection_refresh=1000, timeout_s=10.0, max_runtime_min=1, max_consecutive_fails=3,
        max_row_bytes=32_768, expected_dim=4, lockfile=None, no_augment_anchors=True,
        subdivide_oversize=True, dry_run=False,
    )  # fmt: skip


def test_backfill_runs_the_owed_check_once_and_leaves_other_rows_alone(ids, monkeypatch):
    import embed_backfill as eb
    import memory_core as mc

    backend, m = ids
    _insert(backend, m["old"], "deploys go to staging first", vec=VEC)  # the fact to be superseded
    _insert(backend, m["new"], "deploys go straight to production", meta={"contradiction_check": "deferred"})
    _insert(backend, m["optout"], "deploys go to a canary", meta={})  # opted out: no flag, no vector
    _insert(backend, m["legacy"], "deploys go nowhere", vec=None)

    async def fake_embed_many(texts):
        # The flagged row matches "old"; optout and legacy match EACH OTHER, so if
        # either were (wrongly) checked it would supersede the other.
        return [(VEC if "production" in t else OTHER, "t") for t in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    prefix = m["new"].split("-")[0]
    asyncio.run(eb._run_sweep_inner(_sweep_args(prefix), eb.Counters()))

    where = f"on {backend.name}. inspect: embed_backfill deferred contradiction checks"
    assert _row(backend, m["old"])["is_deleted"] == 1, f"observed: the contradicted memory stayed active {where}"
    new = _row(backend, m["new"])
    assert new["is_deleted"] == 0 and "contradiction_check" not in _meta(new), where
    # optout and legacy were embedded by the same sweep but never checked, so
    # neither superseded the other and both stay active.
    assert _row(backend, m["optout"])["is_deleted"] == 0, where
    assert _row(backend, m["legacy"])["is_deleted"] == 0, where


def test_the_deferred_check_is_a_no_op_the_second_time_and_on_unflagged_rows(ids):
    from memory.write import run_deferred_contradiction_check

    backend, m = ids
    _insert(backend, m["old"], "the build uses make", vec=VEC)
    _insert(backend, m["new"], "the build uses ninja", meta={"contradiction_check": "deferred"}, vec=VEC)
    _insert(backend, m["legacy"], "the build uses bazel", vec=OTHER)  # vector but no flag

    assert asyncio.run(run_deferred_contradiction_check(m["legacy"])) is None
    assert _row(backend, m["old"])["is_deleted"] == 0, "an unflagged row must not be checked"
    assert asyncio.run(run_deferred_contradiction_check(m["new"])) == [m["old"]]
    assert asyncio.run(run_deferred_contradiction_check(m["new"])) is None, "second run must be a no-op"


def test_an_owed_check_survives_a_sweep_that_fails_after_embedding(ids, monkeypatch):
    """The flag is the promise; the sweep that embeds the row must not be its only
    chance to keep it. If a post-sweep step raises, the embedded row is never
    fetched again, so the next sweep must find its check from the flag."""
    import embed_backfill as eb
    import memory_core as mc

    backend, m = ids
    _insert(backend, m["old"], "deploys go to staging first", vec=VEC)
    _insert(backend, m["new"], "deploys go straight to production", meta={"contradiction_check": "deferred"})

    async def fake_embed_many(texts):
        return [(VEC, "t") for _ in texts]

    real_repair, calls = eb._repair_unfindable, []

    async def fails_once(args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("a post-sweep repair failed")
        await real_repair(args)

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    monkeypatch.setattr(eb, "_repair_unfindable", fails_once)
    prefix = m["new"].split("-")[0]
    with pytest.raises(Exception):
        asyncio.run(eb._run_sweep_inner(_sweep_args(prefix), eb.Counters()))
    asyncio.run(eb._run_sweep_inner(_sweep_args(prefix), eb.Counters()))  # the next cycle

    where = f"on {backend.name}. inspect: embed_backfill deferred contradiction checks"
    assert _row(backend, m["old"])["is_deleted"] == 1, f"observed: the owed check never ran {where}"
    assert "contradiction_check" not in _meta(_row(backend, m["new"])), where
