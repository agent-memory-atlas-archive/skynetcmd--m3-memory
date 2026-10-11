"""An MCP write's empty `variant` must not switch off contradiction detection.

The MCP schema default for `variant` is "" (the bridge passes it explicitly), and
untagged rows store variant NULL. memory_write_impl normalised "" to None for the
INSERT but handed the raw "" to the contradiction scan, which then filtered
`mi.variant = ''`: no untagged row can match, so every MCP write skipped
contradiction and supersede detection while a Python caller (variant=None) got it.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3

import pytest


@pytest.mark.parametrize("variant", [None, ""], ids=["python-none", "mcp-empty"])
def test_a_contradicting_write_supersedes_the_old_fact(monkeypatch, variant):
    mc = importlib.import_module("memory_core")
    wr = importlib.import_module("memory.write")
    emb = importlib.import_module("memory.embed")
    from m3_core.paths import resolve_db_path

    vec = [1.0] + [0.0] * 1023

    async def fake_embed(_text, *a, **k):
        return (vec, "test-embed")

    # memory.write binds _embed from memory.embed at import: patch THAT binding
    # (patching memory_core._embed reaches the live embedder instead). Embed
    # inline: with no fast tier the vector is deferred, and a deferred write's
    # contradiction check waits for embed_backfill, after this test ends.
    monkeypatch.setattr(wr, "_embed", fake_embed)
    monkeypatch.setattr(wr, "_get_embedded_embedder", lambda: None)
    monkeypatch.setattr(emb, "fast_embedder_available", lambda: True)

    async def write(content, v):
        return await mc.memory_write_impl(
            type="fact", content=content, title="server port", agent_id="agent-a", variant=v, auto_classify=False
        )

    old_id = _id_of(asyncio.run(write("the server listens on 8080", None)))
    asyncio.run(write("the server listens on 9090", variant))

    with sqlite3.connect(resolve_db_path(None)) as raw:
        edges = raw.execute(
            "SELECT COUNT(*) FROM memory_relationships WHERE to_id = ? AND relationship_type = 'supersedes'", (old_id,)
        ).fetchone()[0]
    assert edges == 1, f"variant={variant!r}: the contradicting write did not supersede {old_id}"


def _id_of(result) -> str:
    import re

    m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(result))
    assert m, f"no id in memory_write result: {result!r}"
    return m.group(0)
