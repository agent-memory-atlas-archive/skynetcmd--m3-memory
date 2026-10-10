"""explain=True explains every result, including rows that skipped scoring.

The exact-substring short-circuit and the no-query-vector fallback return
before the hybrid scorer, so their rows had no _explanation and memory_suggest
printed nothing about why they came back (found by bin/test_memory_bridge.py,
2026-10-10). They now carry a Source.
"""

from __future__ import annotations

import asyncio
import uuid


def test_an_unscored_hit_names_its_source(each_backend, monkeypatch):
    import memory_core
    from memory.write import memory_write_impl

    word = f"zqx{uuid.uuid4().hex[:10]}"
    asyncio.run(memory_write_impl("note", f"{word} explained", title="t", embed=False))
    # No query vector: the degrade path that answers from keywords alone.
    monkeypatch.setattr(memory_core, "_embed", lambda *a, **k: asyncio.sleep(0, result=(None, None)), raising=False)
    out = asyncio.run(memory_core.memory_suggest_impl(word))
    assert word in out, f"observed: {out[:200]!r}"
    assert "Source:" in out or "Breakdown:" in out, (
        f"observed: no explanation on {each_backend.name}: {out[:300]!r}. inspect: search._explain_unscored"
    )
