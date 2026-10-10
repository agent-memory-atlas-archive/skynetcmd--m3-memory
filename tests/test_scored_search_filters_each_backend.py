"""memory_search_scored_impl's row filters hold on EVERY backend.

The SQLite path filters by conversation_id, variant and the requesting-agent
boundary in its own WHERE list; the PostgreSQL path built its filter from the
shared row filter, which did not carry them. A filter that one backend ignores
returns another conversation's (or agent's) rows. Keyword mode, so no embedder
is needed.
"""
from __future__ import annotations

import asyncio
import uuid as _uuid

# Unique per run: the PostgreSQL test store persists, so a fixed word would
# accumulate rows across runs until the k cap hid the new ones.
_WORD = f"zq{_uuid.uuid4().hex[:10]}"


def _write(content: str, **kw) -> str:
    from memory.write import memory_write_impl

    res = asyncio.run(memory_write_impl("note", content, title=content[:20], embed=False, **kw))
    return res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]


def _ids(**kw) -> set[str]:
    from memory.search import memory_search_scored_impl

    rows = asyncio.run(memory_search_scored_impl(query=_WORD, search_mode="keyword", k=20, **kw)) or []
    return {item["id"] for _score, item in rows}


def test_conversation_filter_holds(each_backend):
    a = _write(f"{_WORD} alpha turn", conversation_id="conv-a")
    b = _write(f"{_WORD} beta turn", conversation_id="conv-b")
    got = _ids(conversation_id="conv-a")
    assert a in got and b not in got, f"observed: {got} for conversation conv-a. inspect: the {each_backend.name} search filter"


def test_variant_filter_holds(each_backend):
    a = _write(f"{_WORD} gamma variant", variant="var-x")
    b = _write(f"{_WORD} delta variant", variant="var-y")
    got = _ids(variant="var-x")
    assert a in got and b not in got, f"observed: {got} for variant var-x. inspect: the {each_backend.name} search filter"


def test_agent_isolation_boundary_holds(each_backend):
    """A private (scope='agent') row of agent A is invisible to agent B; a shared
    row stays visible. The security case: it is a boundary between agents."""
    mine = _write(f"{_WORD} private to alpha", agent_id="agent-alpha", scope="agent")
    shared = _write(f"{_WORD} shared with all", agent_id="agent-alpha", scope="user")
    got = _ids(requesting_agent="agent-beta")
    assert shared in got and mine not in got, (
        f"observed: {got} for requesting_agent agent-beta. inspect: the {each_backend.name} isolation filter")
