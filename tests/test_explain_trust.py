"""explain=True says whether to trust each result, not only why it ranked.

The explanation carried relevance terms (vector, bm25, importance, title) but
nothing from the record itself: a superseded, contradicted or graded-unhelpful
memory explained exactly like a corroborated one. And a semantic-only row was
described as a keyword match, because its missing bm25 normalises to 1.0.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from embedding_utils import pack as _pack  # noqa: E402


def test_trust_block_reads_what_the_row_has():
    from memory.search_ranking import _explain_reason, _trust_block

    row = {
        "confidence": 0.9,
        "corroboration_count": 3,
        "contradiction_count": 1,
        "helpful_count": 0,
        "unhelpful_count": 2,
        "pinned": 1,
        "valid_to": "2026-10-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00+00:00",
        "source": "agent",
        "change_agent": "claude",
        "origin_device": "SKYPC",
    }
    t = _trust_block(row)
    assert t["superseded"] is True and t["pinned"] is True and t["corroboration_count"] == 3
    assert t["written_by"] == "claude" and t["device"] == "SKYPC" and t["age_days"] > 200
    reason = _explain_reason({"vector": 0.7, "bm25": 1.0, "keyword_match": False, "trust": t})
    assert "keyword" not in reason, "a semantic-only row is not a keyword match"
    assert "superseded" in reason and "contradicted 1x" in reason and "graded unhelpful" in reason
    assert "corroborated 3x" in reason and "pinned" in reason
    assert _trust_block({"title": "x"}) == {}, "absent columns are not reported as zeros"


@pytest.mark.asyncio
async def test_an_explained_search_carries_the_trust_block(monkeypatch, tmp_path):
    import memory_core
    from memory.config import EMBED_DIM, EMBED_MODEL

    from conftest import create_full_main_schema

    db = tmp_path / "t.db"
    create_full_main_schema(db)
    vec = [1.0] + [0.0] * (EMBED_DIM - 1)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO memory_items (id, type, title, content, source, change_agent, origin_device, created_at,"
            " importance, is_deleted, corroboration_count, helpful_count) VALUES"
            " ('m1','note','t','some text','agent','claude','BOX','2026-01-01T00:00:00Z',0.5,0,2,1)"
        )
        conn.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) VALUES ('m1',?,?,?)",
            (_pack(vec), EMBED_MODEL, EMBED_DIM),
        )

    @contextmanager
    def fake_db(existing=None, *a, **k):
        if existing is not None:
            yield existing
            return
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def fake_embed(_q):
        return (vec, "test-embed")

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_core, "_embed", fake_embed)
    res = await memory_core.memory_search_scored_impl(query="q", k=3, mmr=False, search_mode="semantic", explain=True)
    expl = res[0][1]["_explanation"]
    assert expl["trust"]["corroboration_count"] == 2 and expl["trust"]["written_by"] == "claude"
    assert expl["keyword_match"] is False and "keyword" not in expl["reason"]
    assert "corroborated 2x" in expl["reason"]
