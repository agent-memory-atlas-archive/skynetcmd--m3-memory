"""The bulk-embed cache lookup must hit on every backend (plan item R6).

`_embed_many` first looks up already-stored vectors by content hash, so a bulk
write never re-embeds text it has seen. The lookup hardcoded `?` placeholders,
which PostgreSQL rejects (the PG connection shim deliberately does not
translate them), and swallowed the error at DEBUG -- so on PostgreSQL the cache
never hit and every bulk write re-embedded silently.

The in-process embedder and the HTTP client are replaced by ones that raise: a
cache hit returns before either is reached, a miss reaches one and fails. (The
HTTP stub matters: on a miss the code falls back to the CPU HTTP embedder, and
without it the first version of this test called the real local server.)
"""
from __future__ import annotations

import asyncio

import pytest


class _MustNotEmbed:
    def embed(self, texts):
        raise AssertionError(f"cache missed: embedder called for {len(texts)} text(s)")


def test_cached_vector_is_returned_without_embedding(monkeypatch, each_backend):
    from embedding_utils import pack as _pack
    from memory.backends import dialect
    from memory.textprep import _content_hash
    from memory.write import memory_write_impl

    from conftest import seed_embedding_row
    from memory import embed as me

    monkeypatch.setattr(me, "_get_embedded_embedder", lambda: _MustNotEmbed())

    def _no_http():
        raise AssertionError("cache missed: the HTTP embedder was reached")

    monkeypatch.setattr(me, "_get_embed_client", _no_http)  # no fallback to a live server
    text = "r6 cache probe: the lookup must use the dialect's placeholders"
    vec = [0.25, -0.5, 0.75, 1.0]

    res = asyncio.run(memory_write_impl("note", "r6 parent", title="r6", embed=False))
    mid = res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]
    d = dialect()
    with me._db() as db:
        eid = seed_embedding_row(db, d, mid=mid, embedding=_pack(vec), dim=len(vec),
                                 embed_model=me._EMBED_GGUF_MODEL_TAG)
        db.execute(f"UPDATE memory_embeddings SET content_hash = {d.param()} WHERE id = {d.param()}",
                   (_content_hash(text), eid))
        db.commit()

    out = asyncio.run(me._embed_many([text]))
    assert out[0] is not None
    got, model = out[0]
    assert model == me._EMBED_GGUF_MODEL_TAG
    assert got == pytest.approx(vec)
