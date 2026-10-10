"""memory_link's contract, restored, on EVERY backend.

The Phase 7/8 move into memory/write.py (34ea99a1) dropped the checks
memory_core._memory_link_inner made: the tool accepted an invented type, a
dangling edge, and answered a duplicate (an ON CONFLICT no-op) as "Linked".
Found by bin/test_memory_bridge.py, 2026-10-10.
"""

from __future__ import annotations

import asyncio
import uuid


def _item(tag: str) -> str:
    from memory.write import memory_write_impl

    res = asyncio.run(memory_write_impl("note", f"link probe {tag}", title=tag, embed=False))
    return res.split(":", 1)[1].strip().split()[0]


def test_link_contract(each_backend):
    from memory.write import memory_link_impl

    tag = uuid.uuid4().hex[:8]
    a, b = _item(f"a-{tag}"), _item(f"b-{tag}")
    where = f"on {each_backend.name}. inspect: memory/write.memory_link_impl"

    first = memory_link_impl(a, b, "supports")
    assert first.startswith("Linked: ") and "(id: " in first, f"observed: {first!r} {where}"
    again = memory_link_impl(a, b, "supports")
    assert again.startswith("Link already exists: "), f"observed: {again!r} {where}"

    bad = memory_link_impl(a, b, "invented_type")
    assert bad.startswith("Error:") and "observed: 'invented_type'" in bad, f"observed: {bad!r} {where}"
    dangling = memory_link_impl(a, f"no-such-{tag}", "related")
    assert dangling.startswith("Error: memory not found"), f"observed: {dangling!r} {where}"

    internal = memory_link_impl(a, b, "distills_from")  # written by procedure distillation
    assert internal.startswith("Linked: "), f"observed: {internal!r} {where}"


def test_the_tool_schema_offers_only_public_types():
    from catalog.spec import INTERNAL_RELATIONSHIP_TYPES, VALID_RELATIONSHIP_TYPES

    assert not (INTERNAL_RELATIONSHIP_TYPES & VALID_RELATIONSHIP_TYPES)
    import mcp_tool_catalog as cat

    spec = next(t for t in cat.TOOLS if t.name == "memory_link")
    enum = set(spec.parameters["properties"]["relationship_type"].get("enum", []))
    assert enum == set(VALID_RELATIONSHIP_TYPES), f"observed: memory_link enum {sorted(enum)}"
