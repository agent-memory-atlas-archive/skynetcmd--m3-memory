"""memory_get emits the same shape on EVERY backend: metadata_json is JSON text.

SQLite stores the column as TEXT, so clients json.loads() the field. PostgreSQL
JSONB comes back from the driver as a dict, and memory_get dumped the row as
it was: the field became a nested object and that json.loads() raised
TypeError (found running bin/test_memory_bridge.py on PostgreSQL, 2026-10-10).
"""

from __future__ import annotations

import asyncio
import json


def test_metadata_json_is_text(each_backend):
    from memory.write import memory_write_impl
    from memory_core import memory_get_impl

    res = asyncio.run(
        memory_write_impl(
            "note", "shape probe", title="shape", embed=False, metadata=json.dumps({"k": 1, "nested": {"a": [1, 2]}})
        )
    )
    mid = res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]
    for ident in (mid, mid[:8]):
        out = json.loads(memory_get_impl(ident))
        field = out["metadata_json"]
        assert isinstance(field, str), (
            f"observed: metadata_json is {type(field).__name__} on {each_backend.name}. inspect: memory_core._row_out"
        )
        assert json.loads(field)["nested"] == {"a": [1, 2]}
