"""re_embed_all rewrites embeddings, so it needs a WRITABLE connection.

It opened `open_readonly` (mode=ro on SQLite), so every run failed with "attempt
to write a readonly database" on the default backend; on PostgreSQL the same call
was writable, so the bug showed on one backend only.
"""

from __future__ import annotations

import asyncio
import importlib


def test_re_embed_all_rewrites_the_vector(each_backend, monkeypatch):
    from embedding_utils import unpack
    from memory.backends import dialect
    from memory.write import memory_write_impl

    from memory import db as mdb

    res = asyncio.run(memory_write_impl("note", "re-embed probe body", title="re-embed", embed=False))
    mid = res.split(":", 1)[1].strip() if isinstance(res, str) else res["id"]

    rea = importlib.import_module("re_embed_all")
    new_vec = [0.125, 0.25, 0.5, 1.0]

    async def _fake_embed(text, *a, **k):
        return new_vec, "probe-model"

    # re_embed_all re-embeds through memory.write.rewindow_memory (the write path's
    # windowing owner), so the embedder to replace is the one memory.write uses.
    wr = importlib.import_module("memory.write")
    monkeypatch.setattr(wr, "_embed", _fake_embed)
    monkeypatch.setattr(wr, "_get_embedded_embedder", lambda: None)
    from m3_sdk import resolve_db_path

    asyncio.run(rea.re_embed_all(str(resolve_db_path(None))))

    p = dialect().param()
    with mdb._db() as conn:
        row = conn.execute(
            f"SELECT embedding, embed_model FROM memory_embeddings WHERE memory_id = {p}", (mid,)
        ).fetchone()
    assert row is not None, "observed: no embedding row written. inspect: bin/re_embed_all.py"
    assert row["embed_model"] == "probe-model"
    assert [round(x, 3) for x in unpack(row["embedding"])] == new_vec
