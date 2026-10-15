"""Expansion steps keep another agent's private memories private, on every backend.

A graph edge, a shared conversation or a neighbouring session can join one
agent's private (scope='agent') memory to a shared one. The search filtered its
own candidates by the requesting agent, but the expansion steps hydrated linked
rows with only the user/scope filter, so agent B's search returned agent A's
private memory. Each case also checks that the owner still sees it, so the test
fails if expansion stops returning rows at all.
"""

from __future__ import annotations

import asyncio
import uuid


def _id(res) -> str:
    return res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]


def _ids(rows) -> set[str]:
    return {item["id"] for _score, item in rows if isinstance(item, dict) and item.get("id")}


def _write(content, *, scope, conversation_id="", title=""):
    from memory.write import memory_write_impl

    return _id(
        asyncio.run(
            memory_write_impl(
                "fact",
                content,
                title=title or content[:40],
                agent_id="agent-a",
                scope=scope,
                conversation_id=conversation_id,
                embed=False,
            )
        )
    )


def _routed(query, agent, **kw):
    import memory_core

    return asyncio.run(
        memory_core.memory_search_routed_impl(query, k=5, search_mode="keyword", requesting_agent=agent, **kw)
    )


def test_graph_expansion_does_not_hand_over_a_private_memory(each_backend):
    from memory.write import memory_link_impl

    tag = f"isograph{uuid.uuid4().hex[:8]}"
    shared = _write(f"{tag} shared deployment fact", scope="org")
    private = _write("agent A private note, unrelated words", scope="agent")
    assert "Linked" in memory_link_impl(shared, private, "related")

    other = _ids(_routed(tag, "agent-b", graph_depth=1))
    assert shared in other and private not in other, other

    owner = _ids(_routed(tag, "agent-a", graph_depth=1))
    assert private in owner, owner


def test_session_expansion_does_not_hand_over_a_private_memory(each_backend):
    tag = f"isosess{uuid.uuid4().hex[:8]}"
    cid = f"conv-{uuid.uuid4()}"
    shared = _write(f"{tag} shared turn", scope="org", conversation_id=cid)
    private = _write("agent A private turn, unrelated words", scope="agent", conversation_id=cid)

    other = _ids(_routed(tag, "agent-b", expand_sessions=True))
    assert shared in other and private not in other, other

    owner = _ids(_routed(tag, "agent-a", expand_sessions=True))
    assert private in owner, owner


def test_row_visibility_carries_the_agent_boundary_and_the_window():
    from memory.backends.dialect import dialect_for

    for name in ("sqlite", "postgres"):
        sql, params = dialect_for(name).row_visibility(
            "mi", user_id="u", scope="org", requesting_agent="agent-b", as_of="2026-01-01T00:00:00+00:00"
        )
        assert sql.startswith(" AND (COALESCE(mi.is_deleted, 0) = 0 OR "), (name, sql)
        assert "mi.agent_id" in sql and "agent-b" in params, (name, sql, params)
        assert "mi.valid_to" in sql and "mi.user_id" in sql and "mi.scope" in sql, (name, sql)
