"""Write/update/handoff/inbox results report what they did, on both backends.

- memory_supersede: the replacement inherits user_id/agent_id (else it escapes
  gdpr_forget and tenancy).
- _check_contradictions: ranks every in-scope vector (not an arbitrary LIMIT of
  embedding rows), and an unscoped write never auto-supersedes.
- memory_update: a missing id is an error with no history row; a content edit
  refreshes content_hash; the write gate applies.
- memory_handoff / memory_inbox: counts are real, not assumed.
"""

from __future__ import annotations

import asyncio
import importlib
import uuid

import pytest

VEC = [0.6, 0.0, 0.8, 0.0]
NEAR = [0.6, 0.05, 0.8, 0.0]  # cosine ~0.998 with VEC: above the contradiction threshold
FAR = [0.0, 1.0, 0.0, 0.0]


def _p():
    from memory.backends import dialect

    return dialect().param()


def _new_id(r: str) -> str:
    assert r.startswith("Created:"), r
    return r.split("Created:", 1)[1].split()[0]


def _insert(backend, mid, *, content, vec=None, agent_id="", user_id="", type_="fact", title="t"):
    from embedding_utils import pack

    p = _p()
    model = importlib.import_module("memory.config").EMBED_MODEL
    with backend.connection() as conn:
        conn.execute(
            f"INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, user_id, importance, scope, is_deleted) "
            f"VALUES ({p}, {p}, {p}, {p}, '{{}}', {p}, {p}, 0.5, 'agent', 0)",
            (mid, type_, title, content, agent_id, user_id),
        )
        if vec is not None:
            conn.execute(
                f"INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim) VALUES ({p}, {p}, {p}, {p}, {p})",
                (str(uuid.uuid4()), mid, pack(vec), model, len(vec)),
            )
        conn.commit()


@pytest.fixture
def made(each_backend):
    ids: list[str] = []
    yield each_backend, ids
    p = _p()
    with each_backend.connection() as conn:
        for mid in ids:
            conn.execute(f"DELETE FROM memory_relationships WHERE from_id = {p} OR to_id = {p}", (mid, mid))
            conn.execute(f"DELETE FROM memory_history WHERE memory_id = {p}", (mid,))
            conn.execute(f"DELETE FROM memory_embeddings WHERE memory_id = {p}", (mid,))
            conn.execute(f"DELETE FROM memory_items WHERE id = {p}", (mid,))
        conn.commit()


def _row(backend, mid, cols="*"):
    with backend.connection() as conn:
        return conn.execute(f"SELECT {cols} FROM memory_items WHERE id = {_p()}", (mid,)).fetchone()


def test_supersede_replacement_inherits_owner(made):
    import memory_core as mc

    backend, ids = made
    old = _new_id(
        asyncio.run(
            mc.memory_write_impl(
                type="fact", content=f"old {uuid.uuid4().hex}", title="t", user_id="u-h3", agent_id="a-h3", embed=False
            )
        )
    )
    ids.append(old)
    r = asyncio.run(mc.memory_supersede_impl(old, content=f"new {uuid.uuid4().hex}", embed=False))
    assert "Created:" in r, r
    new = r.split("Created:", 1)[1].split()[0]
    ids.append(new)
    row = _row(backend, new, "user_id, agent_id")
    assert (row["user_id"], row["agent_id"]) == ("u-h3", "a-h3"), f"observed on {backend.name}: {tuple(row)}"


def _check(**kw):
    wmod = importlib.import_module("memory.write")
    return asyncio.run(wmod._check_contradictions(**kw))


def test_unscoped_write_never_supersedes(made):
    backend, ids = made
    old, new = (f"wcb-{uuid.uuid4().hex[:8]}-{k}" for k in ("old", "new"))
    ids += [old, new]
    _insert(backend, old, content="deploy to box A", vec=VEC)
    _insert(backend, new, content="deploy to box B", vec=VEC)
    superseded, _ = _check(item_id=new, content="deploy to box B", title="t", vec=VEC, type_="fact", agent_id="")
    assert superseded == [], f"an unscoped write superseded {superseded} on {backend.name}"
    assert _row(backend, old, "is_deleted")["is_deleted"] == 0


def test_nearest_is_found_beyond_200_decoy_rows(made):
    """The decoys are inserted first, so an unordered LIMIT 200 never saw the
    near match; ranking over every in-scope vector does."""
    backend, ids = made
    agent = f"a-{uuid.uuid4().hex[:8]}"
    for i in range(210):
        mid = f"wcb-{uuid.uuid4().hex[:8]}-d{i}"
        ids.append(mid)
        _insert(backend, mid, content=f"decoy {i}", vec=FAR, agent_id=agent)
    old, new = (f"wcb-{uuid.uuid4().hex[:8]}-{k}" for k in ("old", "new"))
    ids += [old, new]
    _insert(backend, old, content="deploy to box A", vec=NEAR, agent_id=agent)
    _insert(backend, new, content="deploy to box B", vec=VEC, agent_id=agent)
    superseded, _ = _check(item_id=new, content="deploy to box B", title="t", vec=VEC, type_="fact", agent_id=agent)
    assert superseded == [old], f"observed on {backend.name}: {superseded}"


def test_update_of_a_missing_id_is_an_error_and_writes_no_history(made):
    import memory_core as mc

    backend, _ids = made
    missing = str(uuid.uuid4())
    r = asyncio.run(mc.memory_update_impl(missing, content="x"))
    assert r.startswith("Error:") and "not found" in r, r
    with backend.connection() as conn:
        n = conn.execute(f"SELECT COUNT(*) FROM memory_history WHERE memory_id = {_p()}", (missing,)).fetchone()[0]
    assert n == 0, f"{n} history row(s) written for a missing memory on {backend.name}"


def test_content_update_keeps_the_integrity_hash_true(made):
    import memory_core as mc

    _backend, ids = made
    mid = _new_id(asyncio.run(mc.memory_write_impl(type="fact", content="v1 body", title="t", embed=False)))
    ids.append(mid)
    assert asyncio.run(mc.memory_update_impl(mid, content="v2 body")).startswith("Updated:")
    assert mc.memory_verify_impl(mid).startswith("Integrity OK"), mc.memory_verify_impl(mid)
    out = mc.memory_update_bulk_impl([{"id": mid, "content": "v3 body"}])
    assert out["succeeded"] == [mid], out
    assert mc.memory_verify_impl(mid).startswith("Integrity OK"), mc.memory_verify_impl(mid)


def test_update_applies_the_write_gate(made):
    import memory_core as mc

    backend, ids = made
    mid = _new_id(asyncio.run(mc.memory_write_impl(type="fact", content="safe body", title="t", embed=False)))
    ids.append(mid)
    big = "x" * 50_001
    r = asyncio.run(mc.memory_update_impl(mid, content=big))
    assert r.startswith("Error:"), r
    out = mc.memory_update_bulk_impl([{"id": mid, "content": big}])
    assert out["succeeded"] == [] and out["rejected"][0]["id"] == mid, out
    assert _row(backend, mid, "content")["content"] == "safe body"


def test_handoff_counts_only_links_it_wrote(made, monkeypatch):
    import memory_core as mc

    _backend, ids = made
    tag = uuid.uuid4().hex[:8]
    monkeypatch.setattr(mc, "notify_impl", lambda *a, **k: None)
    mc.agent_register_impl(f"wcb{tag}@from", "test")
    mc.agent_register_impl(f"wcb{tag}@to", "test")
    ctx = _new_id(asyncio.run(mc.memory_write_impl(type="fact", content=f"ctx {tag}", title="t", embed=False)))
    ids.append(ctx)
    missing = str(uuid.uuid4())
    out = mc.memory_handoff_impl(
        from_agent=f"wcb{tag}@from", to_agent=f"wcb{tag}@to", task="t", context_ids=[ctx, missing]
    )
    ids.append(out.split("Handoff created:", 1)[1].split()[0])
    assert "1 of 2 context links" in out and missing in out, out


def test_inbox_header_reports_n_of_m(made, monkeypatch):
    import memory_core as mc

    _backend, ids = made
    tag = uuid.uuid4().hex[:8]
    monkeypatch.setattr(mc, "notify_impl", lambda *a, **k: None)
    mc.agent_register_impl(f"wcb{tag}@from", "test")
    mc.agent_register_impl(f"wcb{tag}@to", "test")
    for i in range(3):
        out = mc.memory_handoff_impl(from_agent=f"wcb{tag}@from", to_agent=f"wcb{tag}@to", task=f"t{i}")
        ids.append(out.split("Handoff created:", 1)[1].split()[0])
    inbox = mc.memory_inbox_impl(f"wcb{tag}@to", limit=2)
    assert "(2 of 3 unread)" in inbox, inbox
    import json

    rec = json.loads(mc.memory_inbox_impl(f"wcb{tag}@to", limit=2, as_records=True))
    assert rec["count"] == 2 and rec["total"] == 3, rec
