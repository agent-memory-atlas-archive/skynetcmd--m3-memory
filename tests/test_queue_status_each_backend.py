"""Queue bookkeeping and the null-safe/additive Dialect primitives, on every backend.

- ``Dialect.null_safe_equals``: NULL matches NULL (consolidation's NULL agent/user
  groups depend on it).
- ``Dialect.on_conflict_add(..., set_columns=...)``: one upsert adds to a counter
  and overwrites the rest, so concurrent writers cannot lose an increment.
- fact_enrichment_queue.status: a run that yields no facts is marked 'done' and
  leaves the pending selection, instead of being re-enriched every pass.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest


def test_null_safe_equals_matches_null_to_null(each_backend):
    from memory.backends import dialect

    d = dialect()
    p = d.param()
    with each_backend.connection() as c:
        cur = c.cursor()
        cur.execute("CREATE TEMP TABLE t_nse (k TEXT, v TEXT)")
        cur.execute(f"INSERT INTO t_nse (k, v) VALUES ({p}, {p}), ({p}, {p})", ("a", None, "b", "x"))

        def keys(value):
            cur.execute(f"SELECT k FROM t_nse WHERE {d.null_safe_equals('v', p)} ORDER BY k", (value,))
            return [r[0] for r in cur.fetchall()]

        assert keys(None) == ["a"]
        assert keys("x") == ["b"]
        assert keys("y") == []


def test_on_conflict_add_adds_one_column_and_overwrites_the_rest(each_backend):
    from memory.backends import dialect

    d = dialect()
    p = d.param()
    suffix = d.on_conflict_add("(k)", "t_ocas", ["n"], set_columns=["note"])
    with each_backend.connection() as c:
        cur = c.cursor()
        cur.execute("CREATE TEMP TABLE t_ocas (k TEXT PRIMARY KEY, n INTEGER, note TEXT)")
        cur.execute(f"INSERT INTO t_ocas (k, n, note) VALUES ({p}, NULL, {p})", ("x", "seed"))
        for note in ("first", "second"):
            cur.execute(f"INSERT INTO t_ocas (k, n, note) VALUES ({p}, 1, {p}) {suffix}", ("x", note))
        cur.execute("SELECT n, note FROM t_ocas")
        assert tuple(cur.fetchone()) == (2, "second")  # a NULL counter starts from 0


def test_record_queue_status_counts_attempts_and_overwrites_status(each_backend):
    from memory.entity import record_queue_status
    from memory.write import memory_write_impl

    res = asyncio.run(memory_write_impl("note", f"queue probe {uuid.uuid4().hex}", embed=False))
    mid = res.split(":", 1)[1].strip()
    p = each_backend.dialect().param()
    with each_backend.connection() as c:
        record_queue_status(c, "fact_enrichment_queue", mid, "failed", "boom")
        record_queue_status(c, "fact_enrichment_queue", mid, "done")
        cur = c.cursor()
        cur.execute(f"SELECT attempts, status, last_error FROM fact_enrichment_queue WHERE memory_id = {p}", (mid,))
        assert tuple(cur.fetchone()) == (2, "done", None)
    with pytest.raises(ValueError, match="not a status queue"):
        record_queue_status(None, "memory_items", mid, "done")


def _pending_ids() -> set[str]:
    from memory.db import _db
    from memory.enrich import _select_pending_fact_enrichment

    with _db() as db:
        return {r[0] for r in _select_pending_fact_enrichment(db)}


def _queue_row(each_backend, mid):
    p = each_backend.dialect().param()
    with each_backend.connection() as c:
        cur = c.cursor()
        cur.execute(f"SELECT attempts, status FROM fact_enrichment_queue WHERE memory_id = {p}", (mid,))
        row = cur.fetchone()
    return tuple(row) if row else None


def test_a_run_with_no_facts_is_not_reselected(each_backend):
    from memory.enrich import _run_fact_enricher
    from memory.write import memory_write_impl

    async def no_facts(_content):
        return []

    async def broken(_content):
        raise RuntimeError("enricher down")

    empty = asyncio.run(memory_write_impl("note", f"nothing factual {uuid.uuid4().hex}", embed=False))
    empty = empty.split(":", 1)[1].strip()
    failing = asyncio.run(memory_write_impl("note", f"enricher fails {uuid.uuid4().hex}", embed=False))
    failing = failing.split(":", 1)[1].strip()
    assert {empty, failing} <= _pending_ids()

    out = asyncio.run(_run_fact_enricher(empty, "x", no_facts, sem=asyncio.Semaphore(0)))
    assert out["ok"] and out["facts_written"] == 0
    assert _queue_row(each_backend, empty) == (1, "done")

    out = asyncio.run(_run_fact_enricher(failing, "x", broken, sem=asyncio.Semaphore(0)))
    assert not out["ok"]
    assert _queue_row(each_backend, failing) == (1, "failed")

    pending = _pending_ids()
    assert empty not in pending, "a processed run with no facts must leave the pending selection"
    assert failing in pending, "a failed run stays retry-eligible until the attempts cap"
