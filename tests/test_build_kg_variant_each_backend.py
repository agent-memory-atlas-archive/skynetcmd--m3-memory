"""build_kg_variant copies a variant and links similar items on EVERY backend.

It refused on PostgreSQL (require_sqlite_backend), resolved its store at import
(before --database/--store were parsed), and spelled every bind `?`.
"""

from __future__ import annotations

import struct
import sys
import uuid

import pytest

from conftest import skip_reason

pytest.importorskip("numpy", reason=skip_reason("dep", "numpy is required by build_kg_variant"))


def _vec(*xs: float) -> bytes:
    return struct.pack(f"{len(xs)}f", *xs)


@pytest.fixture
def source(each_backend):
    """Three items in a fresh source variant: a, b near-identical; c orthogonal."""
    from memory.backends import dialect

    p = dialect().param()
    tag = uuid.uuid4().hex[:8]
    src, tgt = f"kg-src-{tag}", f"kg-tgt-{tag}"
    vecs = {"a": _vec(1, 0, 0, 0), "b": _vec(0.99, 0.01, 0, 0), "c": _vec(0, 0, 1, 0)}
    with each_backend.connection() as conn:
        for name, v in vecs.items():
            mid = f"kg-{tag}-{name}"
            conn.execute(
                f"INSERT INTO memory_items (id, type, content, variant, is_deleted) VALUES ({p}, 'note', {p}, {p}, 0)",
                (mid, f"item {name}", src),
            )
            conn.execute(
                f"INSERT INTO memory_embeddings (id, memory_id, embedding, dim) VALUES ({p}, {p}, {p}, 4)",
                (f"kge-{tag}-{name}", mid, v),
            )
        conn.commit()
    try:
        yield src, tgt
    finally:
        with each_backend.connection() as conn:
            for v in (src, tgt):
                ids = f"(SELECT id FROM memory_items WHERE variant = {p})"
                conn.execute(f"DELETE FROM memory_relationships WHERE from_id IN {ids} OR to_id IN {ids}", (v, v))
                conn.execute(f"DELETE FROM memory_embeddings WHERE memory_id IN {ids}", (v,))
                conn.execute(f"DELETE FROM memory_items WHERE variant = {p}", (v,))
            conn.commit()


def _run(monkeypatch, each_backend, *argv: str) -> None:
    import build_kg_variant as kg

    extra: list[str] = []
    if each_backend.stores_databases_as_files:
        from m3_sdk import resolve_db_path

        extra = ["--database", resolve_db_path(None)]
    monkeypatch.setattr(sys, "argv", ["build_kg_variant", *argv, *extra])
    kg.main()


def test_copies_and_links_similar_items(each_backend, source, monkeypatch):
    from memory.backends import dialect

    src, tgt = source
    p = dialect().param()
    where = f"on {each_backend.name}. inspect: bin/build_kg_variant.py"
    args = ("--source-variant", src, "--target-variant", tgt, "--top-n", "1", "--sim-threshold", "0.9")
    _run(monkeypatch, each_backend, *args)

    with each_backend.connection() as conn:
        items = dict(conn.execute(f"SELECT content, id FROM memory_items WHERE variant = {p}", (tgt,)).fetchall())
        n_emb = conn.execute(
            f"SELECT COUNT(*) FROM memory_embeddings WHERE memory_id IN "
            f"(SELECT id FROM memory_items WHERE variant = {p})",
            (tgt,),
        ).fetchone()[0]
        edges = {
            (f, t)
            for f, t in conn.execute(
                f"SELECT from_id, to_id FROM memory_relationships WHERE from_id IN "
                f"(SELECT id FROM memory_items WHERE variant = {p})",
                (tgt,),
            ).fetchall()
        }
    assert sorted(items) == ["item a", "item b", "item c"] and n_emb == 3, f"observed: {items!r}, {n_emb} {where}"
    a, b = items["item a"], items["item b"]
    assert edges == {(a, b), (b, a)}, f"observed: {edges!r} {where}"

    _run(monkeypatch, each_backend, *args, "--wipe-target")  # rebuilds cleanly
    with each_backend.connection() as conn:
        n = conn.execute(f"SELECT COUNT(*) FROM memory_items WHERE variant = {p}", (tgt,)).fetchone()[0]
    assert n == 3, f"observed: {n} items after --wipe-target {where}"
