"""Enrichment eligibility groups conversations on EVERY backend.

m3_enrich runs on PostgreSQL (its state connection is backend-aware), but the
eligibility query used SQLite's json_extract, so on PostgreSQL it failed on the
first statement. The JSON reads now come from the dialect.
"""

from __future__ import annotations

import asyncio
import json


def _turn(conv: str, idx: int, role: str) -> None:
    from memory.write import memory_write_impl

    asyncio.run(
        memory_write_impl(
            "message",
            f"eligibility turn {idx}",
            title=role,
            embed=False,
            conversation_id=conv,
            metadata=json.dumps({"role": role, "turn_index": idx}),
        )
    )


def test_eligible_groups_by_conversation_in_turn_order(each_backend):
    import uuid

    from enrich.eligibility import _query_eligible_groups
    from m3_sdk import resolve_db_path

    conv = f"conv-elig-{uuid.uuid4().hex[:8]}"  # the PG test store persists across runs
    _turn(conv, 1, "assistant")
    _turn(conv, 0, "user")
    groups = _query_eligible_groups(resolve_db_path(None), ("message",), None, conv_filter={conv})
    convs = {c: turns for _uid, c, turns in groups}
    assert conv in convs, f"observed: {list(convs)}. inspect: enrich/eligibility.py on {each_backend.name}"
    roles = [t[2] for t in convs[conv]]
    assert roles == ["user", "assistant"], f"observed: {roles}. inspect: turn_index ordering"
