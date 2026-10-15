"""A point-in-time search finds what was true then, on every backend.

Superseding a memory closes its validity window and marks it deleted. Every
search filtered ``is_deleted = 0`` even with ``as_of``, so a superseded memory
could never be found at a time it was valid. A memory deleted outright keeps its
window open and must stay hidden at any time.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

import pytest


def _new_id(res) -> str:
    return res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]


def _ids(rows) -> set[str]:
    return {item["id"] for _score, item in rows}


def test_as_of_finds_the_superseded_memory_and_not_its_replacement(each_backend):
    import memory_core
    from memory.write import memory_supersede_impl, memory_write_impl

    tag = f"asofprobe{uuid.uuid4().hex[:8]}"
    old = _new_id(asyncio.run(memory_write_impl("fact", f"{tag} colour is blue", title=tag, embed=False)))
    time.sleep(0.05)
    between = datetime.now(UTC).isoformat()
    time.sleep(0.05)
    res = asyncio.run(memory_supersede_impl(old, f"{tag} colour is green", embed=False))
    new = res.rsplit("Created:", 1)[1].strip()

    then = asyncio.run(memory_core.memory_search_scored_impl(tag, k=5, search_mode="keyword", as_of=between))
    assert old in _ids(then), then
    assert new not in _ids(then), then

    now = asyncio.run(memory_core.memory_search_scored_impl(tag, k=5, search_mode="keyword"))
    assert new in _ids(now) and old not in _ids(now), now


def test_as_of_never_shows_a_memory_deleted_outright(each_backend):
    import memory_core
    from memory.write import memory_write_impl

    tag = f"asofdel{uuid.uuid4().hex[:8]}"
    gone = _new_id(asyncio.run(memory_write_impl("fact", f"{tag} was removed", title=tag, embed=False)))
    time.sleep(0.05)
    between = datetime.now(UTC).isoformat()
    memory_core.memory_delete_impl(gone)

    then = asyncio.run(memory_core.memory_search_scored_impl(tag, k=5, search_mode="keyword", as_of=between))
    assert gone not in _ids(then), then


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_live_predicate_admits_only_a_closed_window_under_as_of(backend):
    from memory.backends.dialect import dialect_for

    d = dialect_for(backend)
    assert d.live_predicate("mi") == "mi.is_deleted = 0"
    with_as_of = d.live_predicate("mi", "2026-01-01T00:00:00+00:00")
    assert with_as_of.startswith("(mi.is_deleted = 0 OR ") and "mi.valid_to IS NOT NULL" in with_as_of
    assert d.live_predicate("", null_is_live=True) == "COALESCE(is_deleted, 0) = 0"
