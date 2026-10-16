"""gdpr_forget erases the copies derived from a subject's memories -- entity
mentions, relationship facts, entities only they reference, corroborations,
fact_enriched rows written without a user_id, a separate chatlog store -- and
leaves every other subject's rows, including entities they share, in place.
A wiki hook failure is reported, not hidden behind erased=True."""

from __future__ import annotations

import importlib
import uuid

import pytest

from conftest import gdpr_erase


def _p():
    from memory.backends import dialect

    return dialect().param()


def _ins(conn, table, row: dict):
    p = _p()
    cols = list(row)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join([p] * len(cols))})", list(row.values()))


def _ids(conn, table, col, values):
    p = _p()
    rows = conn.execute(
        f"SELECT {col} AS v FROM {table} WHERE {col} IN ({', '.join([p] * len(values))})", list(values)
    ).fetchall()
    return {r["v"] for r in rows}


@pytest.fixture
def world(each_backend):
    tag = uuid.uuid4().hex[:8]
    A, B = f"subj-a-{tag}", f"subj-b-{tag}"
    m = {k: f"{k}-{tag}" for k in ("mA1", "mA2", "mB", "fA", "fB")}
    e = {k: f"{k}-{tag}" for k in ("eA", "eShared", "eRel")}
    with each_backend.connection() as c:
        for k, user in (("mA1", A), ("mA2", A), ("mB", B)):
            _ins(
                c,
                "memory_items",
                {"id": m[k], "type": "note", "title": k, "content": k, "user_id": user, "is_deleted": 0},
            )
        # Enricher facts: written WITHOUT a user_id, linked fact -> source.
        for k, src in (("fA", "mA1"), ("fB", "mB")):
            _ins(c, "memory_items", {"id": m[k], "type": "fact_enriched", "title": k, "content": k, "is_deleted": 0})
            _ins(
                c,
                "memory_relationships",
                {"id": str(uuid.uuid4()), "from_id": m[k], "to_id": m[src], "relationship_type": "references"},
            )
        for k in e:
            _ins(c, "entities", {"id": e[k], "canonical_name": k, "entity_type": "person"})
        for mem, ent in (("mA1", "eA"), ("mA1", "eShared"), ("mB", "eShared"), ("mA2", "eRel")):
            _ins(c, "memory_item_entities", {"memory_id": m[mem], "entity_id": e[ent]})
        _ins(
            c,
            "entity_relationships",
            {"from_entity": e["eA"], "to_entity": e["eShared"], "predicate": "knows", "source_memory_id": m["mA1"]},
        )
        # eRel is mentioned only by A, but B's memory states a fact about it: kept.
        _ins(
            c,
            "entity_relationships",
            {"from_entity": e["eShared"], "to_entity": e["eRel"], "predicate": "knows", "source_memory_id": m["mB"]},
        )
        for ent in ("eA", "eShared"):
            _ins(c, "entity_embeddings", {"entity_id": e[ent], "embedding": b"\x00" * 8, "dim": 2})
        corr = {k: f"{k}-{tag}" for k in ("c1", "c2", "c3")}
        _ins(c, "memory_corroborations", {"id": corr["c1"], "memory_id": m["mA1"], "source_ref": "agent"})
        _ins(c, "memory_corroborations", {"id": corr["c2"], "memory_id": m["mB"], "source_ref": m["mA2"]})
        _ins(c, "memory_corroborations", {"id": corr["c3"], "memory_id": m["mB"], "source_ref": "agent-x"})
        c.commit()
    yield each_backend, A, B, m, e, corr
    p = _p()
    with each_backend.connection() as c:
        for table, col, vals in (
            ("memory_corroborations", "id", list(corr.values())),
            ("entity_relationships", "source_memory_id", list(m.values())),
            ("entity_embeddings", "entity_id", list(e.values())),
            ("memory_item_entities", "entity_id", list(e.values())),
            ("entities", "id", list(e.values())),
            ("memory_relationships", "from_id", list(m.values())),
            ("memory_items", "id", list(m.values())),
        ):
            c.execute(f"DELETE FROM {table} WHERE {col} IN ({', '.join([p] * len(vals))})", vals)
        c.execute(f"DELETE FROM gdpr_requests WHERE subject_id IN ({p}, {p})", (A, B))
        c.commit()


def test_derived_copies_go_and_other_subjects_rows_stay(world):
    backend, A, _B, m, e, corr = world
    done = gdpr_erase(user_id=A)
    assert done["complete"] is True and done["failures"] == []
    assert done["derived_ids"] == [m["fA"]]
    with backend.connection() as c:
        assert _ids(c, "memory_items", "id", list(m.values())) == {m["mB"], m["fB"]}
        assert _ids(c, "entities", "id", list(e.values())) == {e["eShared"], e["eRel"]}
        assert _ids(c, "entity_embeddings", "entity_id", list(e.values())) == {e["eShared"]}
        assert _ids(c, "memory_item_entities", "memory_id", list(m.values())) == {m["mB"]}
        assert _ids(c, "entity_relationships", "source_memory_id", list(m.values())) == {m["mB"]}
        assert _ids(c, "memory_corroborations", "id", list(corr.values())) == {corr["c3"]}
        assert _ids(c, "memory_relationships", "from_id", list(m.values())) == {m["fB"]}


def test_plan_counts_match_what_the_erasure_removes(world):
    _backend, A, _B, _m, _e, _corr = world
    mm = importlib.import_module("memory_maintenance")
    plan = mm.gdpr_forget_impl(user_id=A)
    assert plan["counts"]["memory_items"] == 3  # mA1, mA2 and the derived fact
    assert plan["counts"]["entities"] == 1 and plan["entities_erased"] == 1
    assert plan["counts"]["memory_corroborations"] == 2
    assert plan["counts"]["entity_relationships"] == 1  # counted once, matched twice


def test_memory_ids_target_keeps_shared_entities(world):
    backend, _A, _B, m, e, _corr = world
    done = gdpr_erase(memory_ids=[m["mA1"]])
    with backend.connection() as c:
        assert _ids(c, "entities", "id", list(e.values())) == {e["eShared"], e["eRel"]}
        assert _ids(c, "memory_items", "id", list(m.values())) == {m["mA2"], m["mB"], m["fB"]}
    assert done["items_erased"] == 2


def test_a_failed_wiki_hook_is_reported_not_hidden(world, monkeypatch):
    backend, A, _B, m, _e, _corr = world
    we = importlib.import_module("wiki.erasure")

    def boom(*a, **k):
        raise RuntimeError("wiki store unreachable")

    monkeypatch.setattr(we, "restrict_derived_on_erasure", boom)
    done = gdpr_erase(user_id=A)
    assert done["erased"] is True and done["complete"] is False
    assert [f["step"] for f in done["failures"]] == ["wiki_restricted"]
    assert "wiki store unreachable" in done["failures"][0]["error"]
    with backend.connection() as c:
        assert _ids(c, "memory_items", "id", list(m.values())) == {m["mB"], m["fB"]}


def test_a_separate_chatlog_store_is_erased_too(tmp_path, monkeypatch):
    """SQLite with the chatlog in its own file: the subject's chat rows there go,
    another subject's stay."""
    from conftest import use_backend

    use_backend("sqlite", None)
    chatlog_config = importlib.import_module("chatlog_config")
    path = str(tmp_path / "split_chatlog.db")
    chatlog_config.pin_chatlog_db(path)
    try:
        mm = importlib.import_module("memory_maintenance")
        factory, _T, _full, topology = mm._gdpr_chatlog_store()
        if factory is None:
            # The file is created on first connect; make it exist, then re-resolve.
            import sqlite3

            sqlite3.connect(path).close()
            factory, _T, _full, topology = mm._gdpr_chatlog_store()
        assert topology == "separate", topology
        tag = uuid.uuid4().hex[:8]
        A, B = f"chat-a-{tag}", f"chat-b-{tag}"
        with mm._gdpr_store_txn(factory) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS memory_items (id TEXT PRIMARY KEY, type TEXT, title TEXT, "
                "content TEXT, user_id TEXT, is_deleted INTEGER DEFAULT 0)"
            )
            c.execute("CREATE TABLE IF NOT EXISTS memory_embeddings (id TEXT, memory_id TEXT, embedding BLOB)")
            for mid, user in (("ca", A), ("cb", B)):
                c.execute("INSERT INTO memory_items (id, type, user_id) VALUES (?, 'chat_log', ?)", (mid + tag, user))
                # The store may already carry the migrated schema (embedding NOT NULL).
                c.execute(
                    "INSERT INTO memory_embeddings (id, memory_id, embedding) VALUES (?, ?, ?)",
                    (mid + tag, mid + tag, b"x"),
                )
        plan = mm.gdpr_forget_impl(user_id=A)
        assert plan["chatlog_store"] == "separate"
        assert plan["chatlog_counts"]["memory_items"] == 1
        done = mm.gdpr_forget_impl(user_id=A, confirm=plan["token"], compliance=dict.fromkeys(plan["requires"], "t"))
        assert done["erased"] and done["chatlog_counts"]["memory_embeddings"] == 1
        with mm._gdpr_store_txn(factory) as c:
            left = {r[0] for r in c.execute("SELECT id FROM memory_items").fetchall()}
            emb = {r[0] for r in c.execute("SELECT memory_id FROM memory_embeddings").fetchall()}
        assert left == {"cb" + tag} and emb == {"cb" + tag}
    finally:
        chatlog_config.pin_chatlog_db(None)


def test_export_includes_retained_and_derived_copies(world):
    import json

    backend, A, _B, m, _e, _corr = world
    p = _p()
    with backend.connection() as c:
        c.execute(f"UPDATE memory_items SET is_deleted = 1 WHERE id = {p}", (m["mA2"],))
        c.commit()
    mm = importlib.import_module("memory_maintenance")
    out = json.loads(mm.gdpr_export_impl(A))
    assert {r["id"] for r in out["items"]} == {m["mA1"], m["mA2"], m["fA"]}
    assert out["live_count"] == 2 and out["derived_ids"] == [m["fA"]]
    assert out["request_logged"] is True and out["request_id"]
