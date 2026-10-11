"""memory_supersede with replacement_id: retire into an EXISTING memory, on every backend.

Writing a fresh successor per duplicate multiplied copies (m3 e116b526: 5
duplicated decisions left 19 active rows for 7). With replacement_id nothing is
created; the old memory is closed through the same _mark_superseded as the
content path (supersedes edge new -> old, closed interval, history row), so the
active-copy count drops by one per call. Each refusal leaves both rows untouched.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest


def _seed(backend, content: str) -> str:
    from memory.backends import dialect

    p = dialect().param()
    mid = str(uuid.uuid4())
    with backend.connection() as conn:
        conn.execute(
            f"INSERT INTO memory_items (id, type, title, content, importance, scope, is_deleted) "
            f"VALUES ({p}, 'decision', {p}, {p}, 0.5, 'agent', 0)",
            (mid, f"dup {content[:20]}", content),
        )
        conn.commit()
    return mid


def _active(backend, ids: list[str]) -> int:
    from memory.backends import dialect

    p = dialect().param()
    with backend.connection() as conn:
        return sum(
            conn.execute(f"SELECT is_deleted FROM memory_items WHERE id = {p}", (i,)).fetchone()[0] == 0 for i in ids
        )


def _count_items(backend) -> int:
    with backend.connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]


@pytest.fixture
def three_copies(each_backend):
    tag = uuid.uuid4().hex[:8]
    ids = [_seed(each_backend, f"the same decision {tag}") for _ in range(3)]
    yield each_backend, ids
    from memory.backends import dialect

    p = dialect().param()
    with each_backend.connection() as conn:
        for i in ids:
            conn.execute(f"DELETE FROM memory_relationships WHERE from_id = {p} OR to_id = {p}", (i, i))
            conn.execute(f"DELETE FROM memory_history WHERE memory_id = {p}", (i,))
            conn.execute(f"DELETE FROM memory_items WHERE id = {p}", (i,))
        conn.commit()


def test_retiring_duplicates_into_the_canonical_copy_lowers_the_active_count(three_copies):
    from memory.backends import dialect
    from memory_core import memory_supersede_impl

    backend, (keep, dup1, dup2) = three_copies
    before_rows = _count_items(backend)
    for dup in (dup1, dup2):
        r = asyncio.run(memory_supersede_impl(old_id=dup, replacement_id=keep))
        assert r == f"Superseded {dup} -> Existing: {keep}", r

    where = f"on {backend.name}. inspect: memory/write.py _supersede_into_existing"
    assert _active(backend, [keep, dup1, dup2]) == 1, f"observed: active copies did not drop to 1 {where}"
    assert _count_items(backend) == before_rows, f"observed: a row was created {where}"
    p = dialect().param()
    with backend.connection() as conn:
        edges = conn.execute(
            f"SELECT from_id, to_id FROM memory_relationships WHERE relationship_type = 'supersedes' "
            f"AND to_id IN ({p}, {p})",
            (dup1, dup2),
        ).fetchall()
        hist = conn.execute(
            f"SELECT COUNT(*) FROM memory_history WHERE event = 'supersede' AND new_value = {p}", (keep,)
        ).fetchone()[0]
    assert sorted((e[0], e[1]) for e in edges) == sorted([(keep, dup1), (keep, dup2)]), f"observed: {edges} {where}"
    assert hist == 2, f"observed: {hist} history rows {where}"


@pytest.mark.parametrize("case", ["self", "missing", "closed", "both"])
def test_each_refusal_changes_nothing(three_copies, case):
    from memory_core import memory_supersede_impl

    backend, (a, b, c) = three_copies
    if case == "closed":  # retire c into a first, then try to retire b into the closed c
        assert asyncio.run(memory_supersede_impl(old_id=c, replacement_id=a)).startswith("Superseded ")
    kwargs = {
        "self": {"old_id": b, "replacement_id": b},
        "missing": {"old_id": b, "replacement_id": str(uuid.uuid4())},
        "closed": {"old_id": b, "replacement_id": c},
        "both": {"old_id": b, "replacement_id": a, "content": "a new body too"},
    }[case]
    before_rows = _count_items(backend)
    r = asyncio.run(memory_supersede_impl(**kwargs))
    assert r.startswith("Error:"), f"observed: {case} was accepted on {backend.name}: {r}"
    assert _active(backend, [b]) == 1, f"observed: {case} closed old_id on {backend.name}"
    assert _count_items(backend) == before_rows, f"observed: {case} created a row on {backend.name}"


def test_the_validator_enforces_exactly_one_and_full_uuids():
    from catalog.validators import _memory_supersede_validator

    old, rep = str(uuid.uuid4()), str(uuid.uuid4())
    assert isinstance(_memory_supersede_validator({"old_id": old, "replacement_id": rep}), dict)
    assert isinstance(_memory_supersede_validator({"old_id": old, "content": "body"}), dict)
    for bad in (
        {"old_id": old},
        {"old_id": old, "content": "body", "replacement_id": rep},
        {"old_id": old, "replacement_id": rep[:8]},
        {"old_id": old, "replacement_id": old},
    ):
        out = _memory_supersede_validator(dict(bad))
        assert isinstance(out, str) and out.startswith("Error:"), (bad, out)
