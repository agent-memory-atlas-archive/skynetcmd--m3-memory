"""One re-window owner: re-embed and the repair store windows as a fresh write does.

Two defects shared one cause, on every backend:
memory_update(reembed=True) and re_embed_all ran a blanket
``UPDATE memory_embeddings SET embedding = ? WHERE memory_id = ?``, writing ONE
vector over every window row; and memories windowed before 2026.9.8.0 carry
window_N rows and no 'default' row, so default search never returns them and
nothing repaired them. memory.write.rewindow_memory now does both jobs.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import uuid

import pytest


def _p():
    from memory.backends import dialect

    return dialect().param()


def _vec_for(text: str) -> list[float]:
    """A distinct 4-d vector per text, so a flattened store is detectable."""
    h = (sum(map(ord, text)) % 97) + 1
    return [float(h), 1.0, 0.0, 0.0]


@pytest.fixture
def embedder(monkeypatch):
    """Two windows per text, each embedded from its own slice (no live embedder)."""
    wr = importlib.import_module("memory.write")

    async def fake_embed(text, *a, **k):
        return _vec_for(text), "test-model"

    def two_windows(text, windows=None):
        mid = len(text) // 2
        return [(text[:mid], 0), (text[mid:], 1)]

    monkeypatch.setattr(wr, "_embed", fake_embed)
    monkeypatch.setattr(wr, "_get_embedded_embedder", lambda: None)
    monkeypatch.setattr(wr, "_chunk_for_sliding_window", two_windows)
    return wr


@pytest.fixture
def windowed_orphan(each_backend):
    """A pre-9.8 memory: window_0 + window_1 rows, no 'default' row."""
    from embedding_utils import pack

    p = _p()
    mid = f"rw{uuid.uuid4().hex[:10]}"
    with each_backend.connection() as conn:
        conn.execute(
            f"INSERT INTO memory_items (id, type, title, content, metadata_json, is_deleted) "
            f"VALUES ({p}, 'note', 'long', {p}, '{{}}', 0)",
            (mid, "first half of a long memory | second half of a long memory"),
        )
        for k in ("window_0", "window_1"):
            conn.execute(
                f"INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, vector_kind) "
                f"VALUES ({p}, {p}, {p}, 'old', 4, {p})",
                (str(uuid.uuid4()), mid, pack([9.0, 9.0, 9.0, 9.0]), k),
            )
        conn.commit()
    yield each_backend, mid
    with each_backend.connection() as conn:
        conn.execute(f"DELETE FROM memory_embeddings WHERE memory_id = {p}", (mid,))
        conn.execute(f"DELETE FROM memory_history WHERE memory_id = {p}", (mid,))
        conn.execute(f"DELETE FROM memory_items WHERE id = {p}", (mid,))
        conn.commit()


def _rows(backend, mid):
    from embedding_utils import unpack

    p = _p()
    with backend.connection() as conn:
        got = conn.execute(
            f"SELECT vector_kind, embedding FROM memory_embeddings WHERE memory_id = {p} ORDER BY vector_kind",
            (mid,),
        ).fetchall()
    return {r[0]: tuple(unpack(r[1])) for r in got}


def test_rewindow_repairs_an_orphan_and_keeps_windows_distinct(windowed_orphan, embedder):
    backend, mid = windowed_orphan
    assert asyncio.run(embedder.rewindow_memory(mid)) == 2
    rows = _rows(backend, mid)
    where = f"on {backend.name}. inspect: memory.write.rewindow_memory"
    assert set(rows) == {"default", "window_1"}, f"observed: {sorted(rows)} {where}"
    assert rows["default"] != rows["window_1"], f"observed: windows flattened to one vector {where}"


def test_memory_update_reembed_rewindows_instead_of_flattening(windowed_orphan, embedder):
    import memory_core as mc

    backend, mid = windowed_orphan
    r = asyncio.run(mc.memory_update_impl(id=mid, content="alpha alpha alpha | omega omega omega", reembed=True))
    assert r.startswith("Updated"), r
    rows = _rows(backend, mid)
    assert set(rows) == {"default", "window_1"}, f"observed: {sorted(rows)} on {backend.name}"
    assert rows["default"] != rows["window_1"], f"observed: re-embed flattened the windows on {backend.name}"


def test_no_vector_keeps_the_existing_rows(windowed_orphan, embedder, monkeypatch):
    backend, mid = windowed_orphan
    before = _rows(backend, mid)

    async def nothing(text, *a, **k):
        return None, ""

    monkeypatch.setattr(embedder, "_embed", nothing)
    assert asyncio.run(embedder.rewindow_memory(mid)) == 0
    assert _rows(backend, mid) == before, f"observed: rows changed although nothing was embedded on {backend.name}"


def test_backfill_repairs_orphans_then_finds_none(windowed_orphan, embedder):
    import embed_backfill as eb
    from doctor.embed_space_probe import unfindable_memories
    from m3_sdk import resolve_db_path
    from memory.backends import dialect

    backend, mid = windowed_orphan
    args = argparse.Namespace(db=resolve_db_path(None), dry_run=False)

    def orphans():
        with backend.connection() as conn:
            return {r[0] for r in unfindable_memories(conn, dialect(), limit=100_000)}

    assert mid in orphans()
    asyncio.run(eb._repair_unfindable(args))
    assert mid not in orphans(), f"observed: still unfindable after the repair on {backend.name}"
    assert "default" in _rows(backend, mid)
    asyncio.run(eb._repair_unfindable(args))  # nothing left: a no-op
    assert set(_rows(backend, mid)) == {"default", "window_1"}


def test_a_text_edit_refreshes_the_vector_without_reembed(windowed_orphan, embedder):
    """Editing a memory's text must not leave a vector of the old text: measured
    2026-10-18, 44 live memories still matched their pre-edit wording because
    memory_update re-embedded only with reembed=True."""
    import memory_core as mc

    backend, mid = windowed_orphan
    before = _rows(backend, mid)
    r = asyncio.run(mc.memory_update_impl(id=mid, content="new wording entirely | about something else"))
    assert r.startswith("Updated"), r
    after = _rows(backend, mid)
    assert after and after != before, f"observed: the old vectors survived a text edit on {backend.name}"
    assert set(after) == {"default", "window_1"}


def test_a_metadata_only_edit_keeps_the_vector(windowed_orphan, embedder, monkeypatch):
    import memory_core as mc

    backend, mid = windowed_orphan
    before = _rows(backend, mid)
    calls = []

    async def counting(text, *a, **k):
        calls.append(text)
        return _vec_for(text), "test-model"

    monkeypatch.setattr(embedder, "_embed", counting)
    asyncio.run(mc.memory_update_impl(id=mid, title="a new title", importance=0.9))
    assert calls == [] and _rows(backend, mid) == before, f"observed: a title edit re-embedded on {backend.name}"


def test_a_text_edit_with_no_embedder_leaves_no_stale_vector(windowed_orphan, embedder, monkeypatch):
    """The embedder is down: the stale vectors go anyway, so the embed backfill
    (which embeds every memory without a vector) computes fresh ones later."""
    import memory_core as mc

    backend, mid = windowed_orphan

    async def nothing(text, *a, **k):
        return None, ""

    monkeypatch.setattr(embedder, "_embed", nothing)
    asyncio.run(mc.memory_update_impl(id=mid, content="edited while the embedder was down"))
    assert _rows(backend, mid) == {}, f"observed: a stale vector kept after a text edit on {backend.name}"


def test_bulk_text_edits_drop_stale_vectors_for_the_backfill(windowed_orphan, embedder):
    import memory_core as mc

    backend, mid = windowed_orphan
    out = mc.memory_update_bulk_impl([{"id": mid, "content": "bulk edited text"}])
    assert mid in out["succeeded"], out
    assert _rows(backend, mid) == {}, f"observed: bulk edit kept the old vectors on {backend.name}"


def test_backfill_reembeds_memories_edited_after_their_vector(windowed_orphan, embedder):
    """A memory edited before this release (history newer than its vectors) is
    found by the doctor's predicate, re-embedded by the backfill, then not found."""
    import embed_backfill as eb
    from doctor.embed_space_probe import stale_vector_memories
    from m3_sdk import resolve_db_path
    from memory.backends import dialect

    backend, mid = windowed_orphan
    p = _p()
    asyncio.run(embedder.rewindow_memory(mid))
    with backend.connection() as conn:  # vectors from 2000, then an edit in 2001
        conn.execute(
            f"UPDATE memory_embeddings SET created_at = {p} WHERE memory_id = {p}", ("2000-01-01T00:00:00+00:00", mid)
        )
        conn.execute(
            f"INSERT INTO memory_history (id, memory_id, event, prev_value, new_value, field, actor_id, created_at) "
            f"VALUES ({p}, {p}, 'update', 'old', 'new', 'content', 'test', {p})",
            (str(uuid.uuid4()), mid, "2001-01-01T00:00:00+00:00"),
        )
        conn.commit()

    def stale():
        with backend.connection() as conn:
            return set(stale_vector_memories(conn, dialect(), limit=100_000))

    assert mid in stale(), f"observed: an edit newer than the vectors was not found on {backend.name}"
    asyncio.run(eb._repair_stale_after_edit(argparse.Namespace(db=resolve_db_path(None), dry_run=False)))
    assert mid not in stale(), f"observed: the repair did not re-embed it on {backend.name}"
