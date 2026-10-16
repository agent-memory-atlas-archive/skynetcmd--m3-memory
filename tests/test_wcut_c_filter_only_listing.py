"""memory_search_scored with an empty query lists rows by the filters.

The ToolSpec documents ``query=""`` as a filter-only (type/scope) listing; the
impl returned [] for it, so an agent listing a user's facts got nothing. The
listing must use the search's own tenancy and liveness: another user's rows,
another agent's private rows and deleted rows stay out.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid


def _write(content: str, type_: str = "user_fact", **kw) -> str:
    from memory.write import memory_write_impl

    res = asyncio.run(memory_write_impl(type_, content, title=content[:20], embed=False, **kw))
    return res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]


def _list(**kw) -> list[tuple[float, dict]]:
    from memory.search import memory_search_scored_impl

    return asyncio.run(memory_search_scored_impl(**kw)) or []


def test_empty_query_lists_filtered_rows_by_importance(each_backend):
    # Unique per run: the PostgreSQL test store persists across runs.
    user = f"u-{_uuid.uuid4().hex[:10]}"
    other = f"u-{_uuid.uuid4().hex[:10]}"
    low = _write("prefers tea", user_id=user, scope="user", importance=0.3)
    high = _write("lives in Lisbon", user_id=user, scope="user", importance=0.9)
    note = _write("a plain note", type_="note", user_id=user, scope="user", importance=0.95)
    foreign = _write("someone else's fact", user_id=other, scope="user", importance=1.0)
    gone = _write("deleted fact", user_id=user, scope="user", importance=0.8)
    from memory_core import memory_delete_impl

    res = memory_delete_impl(gone)
    if asyncio.iscoroutine(res):
        asyncio.run(res)

    for q in ("", "   ", "*"):
        rows = _list(query=q, user_id=user, type_filter="user_fact", scope="user", k=10)
        ids = [item["id"] for _s, item in rows]
        assert ids == [high, low], (
            f"observed: {ids} for query={q!r} on {each_backend.name}; expected [high, low] "
            f"(note {note}, foreign {foreign}, deleted {gone} must be absent)"
        )
        assert [round(s, 2) for s, _ in rows] == [0.9, 0.3]


def test_empty_query_respects_k_and_agent_isolation(each_backend):
    user = f"u-{_uuid.uuid4().hex[:10]}"
    mine = _write("shared fact", user_id=user, scope="user", agent_id="agent-a", importance=0.5)
    private = _write("private to a", user_id=user, scope="agent", agent_id="agent-a", importance=0.9)
    rows = _list(query="", user_id=user, requesting_agent="agent-b", k=10)
    ids = {item["id"] for _s, item in rows}
    assert mine in ids and private not in ids, f"observed: {ids} on {each_backend.name}"
    assert len(_list(query="", user_id=user, k=1)) == 1
