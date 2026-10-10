"""The multi-stage queue primitives (m3_enrich_stage) work on EVERY backend.

The module called itself SQLite-only "by design" on the claim that enrichment
short-circuits on PostgreSQL. It does not: enrich/prep.py skips only the SQLite
DDL, because PostgreSQL gets both queues (with `stage`, pg_044) from
migrations. SQL now comes from the dialect of the connection handed in.
"""

from __future__ import annotations

import uuid

import pytest

TABLES = ("observation_queue", "reflector_queue")


@pytest.fixture
def queued(each_backend, request):
    """Two rows under a stage name unique to this run; removed afterwards.

    The PG test store persists across runs, and a leftover queue row is real
    work to the next drainer, so cleanup is not optional."""
    from memory.backends import dialect

    table = request.param
    stage = f"probe_{uuid.uuid4().hex[:8]}"
    p = dialect().param()
    convs = [f"conv-{stage}-{i}" for i in range(2)]
    with each_backend.connection() as conn:
        for c in convs:
            conn.execute(
                f"INSERT INTO {table} (conversation_id, user_id, stage) VALUES ({p}, {p}, {p})", (c, "u-stage", stage)
            )
        conn.commit()
    try:
        yield table, stage, convs
    finally:
        with each_backend.connection() as conn:
            conn.execute(f"DELETE FROM {table} WHERE stage = {p}", (stage,))
            conn.commit()


@pytest.mark.parametrize("queued", TABLES, indirect=True)
def test_pop_fail_depth_ack(each_backend, queued):
    import m3_enrich_stage as st

    table, stage, convs = queued
    where = f"on {each_backend.name}/{table}. inspect: bin/m3_enrich_stage.py"
    with each_backend.connection() as db:
        rows = st.pop_batch(table, stage, 10, db)
        assert sorted(r.conversation_id for r in rows) == convs, f"observed: {rows!r} {where}"
        assert all(r.user_id == "u-stage" and r.attempts == 0 for r in rows)

        first = rows[0]
        for _ in range(st.MAX_ATTEMPTS):
            st.fail(table, first.queue_id, "boom " * 200, db)
        assert st.queue_depth(table, stage, db) == 1, f"observed: a poisoned row still counted {where}"
        assert [r.queue_id for r in st.pop_batch(table, stage, 10, db)] == [rows[1].queue_id]

        st.ack(table, rows[1].queue_id, db)
        assert st.queue_depth(table, stage, db) == 0, f"observed: ack left the row {where}"


def test_an_unknown_table_is_refused_with_the_idiom():
    import m3_enrich_stage as st

    with pytest.raises(ValueError, match=r"observed: 'memory_items'.*inspect:"):
        st.queue_depth("memory_items", "observer", object())
