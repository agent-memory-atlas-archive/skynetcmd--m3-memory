"""Paths that wrote '?' into SQL text run on every backend.

PostgreSQL rejects '?' (the PG connection shim does not translate it), and
several of these paths swallowed the error at DEBUG, so on PostgreSQL they
failed with no visible sign: the single-text embed cache never hit, the window
chunk never fired, the change_agent backfill logged and moved on. Each test
drives one converted path under each_backend and asserts that it ran, not that
it was swallowed. The static ban is tests/test_no_hand_built_placeholders.py.
"""

from __future__ import annotations

import asyncio
import logging

import pytest


def _note(title: str) -> str:
    from memory.write import memory_write_impl

    res = asyncio.run(memory_write_impl("note", f"{title} body", title=title, embed=False))
    return res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]


def test_timeline_query_runs(each_backend):
    from memory.history import get_bitemporal_timeline_impl

    out = get_bitemporal_timeline_impl(_note("timeline probe"))
    assert isinstance(out, str) and out


def test_single_text_embed_cache_hits(monkeypatch, each_backend):
    from embedding_utils import pack as _pack
    from memory.backends import dialect
    from memory.textprep import _content_hash

    from conftest import seed_embedding_row
    from memory import embed as me

    class _MustNotEmbed:
        def embed(self, texts):
            raise AssertionError(
                "cache missed; observed: the embedder was called. inspect: memory/embed.py _embed cache lookup"
            )

    def _no_http():
        raise AssertionError(
            "cache missed; observed: the HTTP embedder was reached. inspect: memory/embed.py _embed cache lookup"
        )

    monkeypatch.setattr(me, "_get_embedded_embedder", lambda: _MustNotEmbed())
    monkeypatch.setattr(me, "_get_embed_client", _no_http)
    text = "single-text cache probe: binds come from the dialect"
    vec = [0.5, -0.25, 1.0, 0.75]
    mid = _note("cache parent")
    d = dialect()
    with me._db() as db:
        eid = seed_embedding_row(
            db, d, mid=mid, embedding=_pack(vec), dim=len(vec), embed_model=me._EMBED_GGUF_MODEL_TAG
        )
        db.execute(
            f"UPDATE memory_embeddings SET content_hash = {d.param()} WHERE id = {d.param()}",
            (_content_hash(text), eid),
        )
        db.commit()

    got, model = asyncio.run(me._embed(text))
    assert model == me._EMBED_GGUF_MODEL_TAG
    assert got == pytest.approx(vec)


@pytest.mark.parametrize("emitter", ["_maybe_emit_window_chunk", "_maybe_emit_gist_row"])
def test_conversation_emitter_query_runs(each_backend, caplog, emitter):
    from memory import emitters

    with caplog.at_level(logging.DEBUG, logger="memory.emitters"):
        asyncio.run(getattr(emitters, emitter)("conv-without-turns", ""))
    failed = [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]
    assert not failed, f"{emitter} swallowed a query failure: {failed}"


def test_change_agent_backfill_runs(each_backend, caplog):
    from memory.db import _backfill_change_agent

    with caplog.at_level(logging.WARNING):
        _backfill_change_agent()
    assert not [r for r in caplog.records if "Backfill failed" in r.getMessage()]


def test_chatlog_scan_where_renders_the_active_dialect(each_backend):
    from chatlog_core import chatlog_scan_where
    from memory.backends import dialect

    where, params = chatlog_scan_where("c1", "2026-01-01", "2026-02-01")
    assert where.count(dialect().param()) == 3 and len(params) == 3
    assert "?" not in where or dialect().param() == "?"
