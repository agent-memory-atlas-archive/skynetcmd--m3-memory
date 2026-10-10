"""conversation_search pairs each user-turn hit with the next turn, on every backend.

The pairing query built its IN-list as `",".join("?" * n)`. PostgreSQL rejects
'?' (the PG shim deliberately does not translate it), and nothing caught the
error, so on PostgreSQL conversation_search failed whenever a hit was a user
turn carrying a turn_index. The search itself is stubbed so only the pairing
query touches the store.
"""

from __future__ import annotations

import asyncio
import json
import uuid


def test_user_hit_is_paired_with_the_next_turn(monkeypatch, each_backend):
    import memory_core
    from catalog import dispatch
    from memory.write import memory_write_impl

    cid = f"conv-{uuid.uuid4()}"

    def _write(content, role, turn):
        res = asyncio.run(
            memory_write_impl(
                "message",
                content,
                title=f"{role}-{turn}",
                embed=False,
                conversation_id=cid,
                metadata=json.dumps({"role": role, "turn_index": turn}),
            )
        )
        return res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]

    user_id = _write("what is the pairing probe?", "user", 0)
    _write("the assistant reply that pairing must pull in", "assistant", 1)

    async def _ranked(query, k=8, **kw):
        return [
            (
                0.9,
                {
                    "id": user_id,
                    "content": "what is the pairing probe?",
                    "title": "user-0",
                    "metadata_json": json.dumps({"role": "user", "turn_index": 0}),
                    "conversation_id": cid,
                },
            )
        ]

    monkeypatch.setattr(memory_core, "memory_search_scored_impl", _ranked)
    out = asyncio.run(dispatch._conversation_search_impl("pairing probe", k=8))
    assert "the assistant reply that pairing must pull in" in out, out
