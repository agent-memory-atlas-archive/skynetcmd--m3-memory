"""Queue drains report what they observed and never lose or re-feed work.

Entity extraction: a zero-entity success is marked 'done' (not deleted, which
made it eligible forever); a fallback-tagged extractor result is a failure that
is retried, not stored as extracted; both selectors share one terminal
predicate. Fact enrichment: rows inherit the source's tenancy, ``limit=0``
means no limit, and execution without an enricher is an error, not a claimed
drain. Reflector prefix matching rejects empty/short references; the batch
runner parses the nested errored-result envelope and reconciles missing results.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _new_id(res: str) -> str:
    assert isinstance(res, str) and res.startswith("Created:"), res
    return res.split(":", 1)[1].strip().split()[0]


def _write(content: str, **kw) -> str:
    from memory.write import memory_write_impl

    return _new_id(asyncio.run(memory_write_impl("note", content, title=content[:20], embed=False, **kw)))


async def _run_extractor(mid: str, content: str, extractor):
    ent = importlib.import_module("memory.entity")
    await ent._ENTITY_EXTRACT_SEM.acquire()  # released by _run_entity_extractor
    return await ent._run_entity_extractor(mid, content, extractor)


def _queue_row(mid: str):
    from memory.backends import dialect
    from memory.db import _db

    with _db() as db:
        return db.execute(
            f"SELECT status, attempts, last_error FROM entity_extraction_queue WHERE memory_id = {dialect().param()}",
            (mid,),
        ).fetchone()


def _selected(mid: str) -> bool:
    ent = importlib.import_module("memory.entity")
    from memory.db import _db

    with _db() as db:
        return mid in {r[0] for r in ent._select_pending_entity_extraction(db)}


# ── entity extraction (both backends) ───────────────────────────────────────


def test_zero_entity_success_is_marked_done_and_not_reselected(each_backend):
    mid = _write(f"nothing nameable here {uuid.uuid4().hex}")
    assert _selected(mid)

    async def empty(_content):
        return {"entities": [], "relationships": []}

    out = asyncio.run(_run_extractor(mid, "x", empty))
    assert out["ok"] is True and out["entities_linked"] == 0, out
    row = _queue_row(mid)
    assert row is not None and row[0] == "done", row
    assert not _selected(mid), "a processed zero-entity memory was re-selected (re-sent forever)"


def test_fallback_tagged_result_is_a_retried_failure_not_an_extraction(each_backend):
    ent = importlib.import_module("memory.entity")
    mid = _write(f"Alice met Bob {uuid.uuid4().hex}")

    async def fell_back(_content):
        return {
            "entities": [{"canonical_name": "Alice", "entity_type": "person"}],
            "relationships": [],
            ent.EXTRACTOR_FALLBACK_KEY: "LLM extractor failed: boom",
        }

    out = asyncio.run(_run_extractor(mid, "x", fell_back))
    assert out["ok"] is False and "substitute" in out["error"], out
    assert out["entities_linked"] == 0
    row = _queue_row(mid)
    assert row[0] == "failed" and row[1] == 1 and "boom" in row[2], row
    assert _selected(mid), "a failure under the retry budget must stay eligible"


def test_done_marker_from_the_batch_tool_is_honoured_by_the_live_drain(each_backend):
    # m3_entities writes status='done' with attempts=1; the live selector used to
    # ignore status and re-select it.
    ent = importlib.import_module("memory.entity")
    from memory.db import _db

    mid = _write(f"batch processed {uuid.uuid4().hex}")
    with _db() as db:
        ent.record_entity_queue_status(db, mid, "done")
    assert _queue_row(mid)[:2] == ("done", 1)
    assert not _selected(mid)


# ── entity extraction drain counts (SQLite: the drain processes every pending row) ──


@pytest.fixture
def sqlite_store(tmp_path, monkeypatch):
    from m3_core.paths import pin_database

    from conftest import create_full_main_schema

    # The schema is pre-created: skip the migrator (what M3_SKIP_MIGRATIONS does).
    monkeypatch.setattr(importlib.import_module("memory.db"), "_ensure_sync_tables", lambda *a, **k: None)
    db = tmp_path / "main.db"
    create_full_main_schema(db)
    pin_database(str(db))
    cfg = importlib.import_module("memory.config")
    monkeypatch.setattr(cfg, "DB_PATH", str(db))
    ent = importlib.import_module("memory.entity")

    async def fake_embed(_name):
        return None

    monkeypatch.setattr(ent, "_embed_canonical_cached", fake_embed)
    return db


def test_extract_pending_reports_observed_counts(sqlite_store, monkeypatch):
    ent = importlib.import_module("memory.entity")
    ext_mod = importlib.import_module("memory.extraction")
    good = _write("Carol works on m3")
    bad = _write("this one raises")

    class _Ext:
        async def extract(self, content):
            if "raises" in content:
                raise RuntimeError("extractor down")
            return {"entities": [{"canonical_name": "Carol", "entity_type": "person"}], "relationships": []}

    monkeypatch.setattr(ext_mod, "get_configured_extractor", lambda: _Ext())
    res = asyncio.run(ent.extract_pending_impl(dry_run=False, limit=0))
    assert res["processed"] == 2 and res["succeeded"] == 1 and res["failed"] == 1, res
    assert res["entities_created"] == 1 and res["entities_linked"] == 1, res
    assert bad in res["errors_summary"] and "extractor down" in res["errors_summary"]
    assert _queue_row(good)[0] == "done"


def test_pending_limit_zero_means_no_limit():
    ent = importlib.import_module("memory.entity")
    assert ent.pending_limit(0) is None
    assert ent.pending_limit(None) is None
    assert ent.pending_limit(-3) is None
    assert ent.pending_limit(5) == 5


# ── fact enrichment ─────────────────────────────────────────────────────────


def test_enrich_pending_without_an_enricher_is_an_error(sqlite_store):
    enrich = importlib.import_module("memory.enrich")
    _write("some content to enrich")
    res = asyncio.run(enrich.enrich_pending_impl(dry_run=False))
    assert "error" in res and res["processed"] == 0 and res["pending"] == 1, res
    # limit=0 is "no limit", not LIMIT 0
    assert asyncio.run(enrich.enrich_pending_impl(dry_run=True, limit=0))["count"] == 1


def test_fact_rows_inherit_source_tenancy_and_are_counted(sqlite_store):
    enrich = importlib.import_module("memory.enrich")
    src = _write("Dana prefers tea", user_id="user-dana", agent_id="agent-x")

    async def enricher(_content):
        return [{"text": "Dana prefers tea", "confidence": 0.9}, {"text": "  "}]

    res = asyncio.run(enrich.enrich_pending_impl(dry_run=False, fact_enricher=enricher))
    assert res["succeeded"] == 1 and res["facts_written"] == 1, res
    con = sqlite3.connect(sqlite_store)
    rows = con.execute(
        "SELECT mi.user_id, mi.agent_id FROM memory_items mi JOIN memory_relationships r ON r.from_id = mi.id "
        "WHERE r.to_id = ? AND mi.type = 'fact_enriched'",
        (src,),
    ).fetchall()
    con.close()
    assert rows == [("user-dana", "agent-x")], rows


# ── reflector text matching ─────────────────────────────────────────────────


def test_reflector_prefix_match_rejects_empty_and_short_references():
    import run_reflector

    rows = [("id-1", {"text": "User has lived in Seattle for five years."})]
    assert run_reflector._find_observation_id_by_text(rows, "") is None
    assert run_reflector._find_observation_id_by_text(rows, "user") is None
    assert run_reflector._find_observation_id_by_text(rows, "User has lived in Seattle") == "id-1"


# ── batch runner ────────────────────────────────────────────────────────────


def test_anthropic_errored_result_nested_envelope_is_parsed():
    import batch_runner

    runner = batch_runner.AnthropicBatchRunner.__new__(batch_runner.AnthropicBatchRunner)
    nested = {
        "custom_id": "g1_c0",
        "result": {
            "type": "errored",
            "error": {"type": "error", "error": {"type": "invalid_request_error", "message": "bad max_tokens"}},
        },
    }
    r = runner._parse_one(nested)
    assert not r.succeeded and r.error == "invalid_request_error: bad max_tokens", r
    flat = {"custom_id": "g1_c1", "result": {"type": "errored", "error": {"type": "overloaded_error", "message": "x"}}}
    assert runner._parse_one(flat).error == "overloaded_error: x"
    expired = {"custom_id": "g1_c2", "result": {"type": "expired"}}
    assert runner._parse_one(expired).error == "expired"


def test_run_to_completion_records_missing_results_as_failures():
    import batch_runner

    class _Runner:
        async def submit(self, requests, *, client):
            return "b1"

        async def poll(self, batch_id, *, client):
            return batch_runner.BatchStatus(batch_id=batch_id, state="ended", n_succeeded=1)

        async def fetch_results(self, batch_id, *, client):
            yield batch_runner.BatchResult(custom_id="a", succeeded=True, text="ok")

    reqs = [batch_runner.BatchRequest(custom_id=c, system="s", user_text="u") for c in ("a", "b")]
    _bid, results = asyncio.run(batch_runner.run_to_completion(_Runner(), reqs, client=None, poll_interval_s=0))
    by_id = {r.custom_id: r for r in results}
    assert by_id["a"].succeeded and not by_id["b"].succeeded
    assert by_id["b"].error.startswith("no_result"), by_id["b"]


class _FakeConn:
    """Answers the drain's queue/turn SELECTs and records every write."""

    def __init__(self, log, queue_rows, turns):
        self.log, self.queue_rows, self.turns = log, queue_rows, turns
        self._last: list = []

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT") and "_queue" in flat:
            self._last = self.queue_rows
        elif flat.startswith("SELECT"):
            self.log.append(("turns", flat, params))
            self._last = self.turns
        else:
            self.log.append(("write", flat, params))
            self._last = []
        return self

    def fetchall(self):
        return list(self._last)

    def commit(self):
        pass


def _fake_db(monkeypatch, mod, log, queue_rows, turns=()):
    from contextlib import contextmanager

    @contextmanager
    def fake(*_a, **_k):
        yield _FakeConn(log, queue_rows, list(turns))

    monkeypatch.setattr(mod.mc, "_db", fake)


def _writes(log, table):
    return [w for w in log if w[0] == "write" and table in w[1]]


@pytest.mark.parametrize("outcome", ["failed", "written"])
def test_observer_drain_keeps_a_failed_row_and_deletes_a_finished_one(monkeypatch, outcome):
    from types import SimpleNamespace

    import run_observer
    import unified_ai

    log: list = []
    _fake_db(monkeypatch, run_observer, log, [(7, "conv-1", "alice", 0)], turns=[("t1", "hi", "user", 0, "", "{}")])

    async def proc(*_a, **_k):
        return {"outcome": outcome, "last_error": "chunk boom" if outcome == "failed" else None}

    monkeypatch.setattr(run_observer, "process_conversation", proc)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(unified_ai, "async_client_for_profile", lambda _p: _Client())
    asyncio.run(run_observer.drain_queue_mode(SimpleNamespace(concurrency=1, batch=10, target_variant=""), None, "t"))

    turns_q = next(w for w in log if w[0] == "turns")
    assert turns_q[2] == ("conv-1", "alice"), "turns must be scoped to the queued user"
    assert "variant IS NULL" in turns_q[1]
    deletes = [w for w in _writes(log, "observation_queue") if w[1].startswith("DELETE")]
    bumps = [w for w in _writes(log, "observation_queue") if w[1].startswith("UPDATE")]
    if outcome == "failed":
        assert not deletes and len(bumps) == 1 and bumps[0][2][0] == "chunk boom", log
    else:
        assert len(deletes) == 1 and not bumps, log


def test_reflector_drain_keeps_a_failed_row(monkeypatch):
    from types import SimpleNamespace

    import run_reflector

    log: list = []
    _fake_db(monkeypatch, run_reflector, log, [(3, "conv-1", "alice", 0)])

    async def refl(*_a, **_k):
        return {"outcome": "failed", "error": "RuntimeError: llm down", "superseded": 0}

    monkeypatch.setattr(run_reflector, "reflect_conversation", refl)
    asyncio.run(run_reflector.drain_queue_mode(SimpleNamespace(concurrency=1, batch=10), None, "t"))
    writes = _writes(log, "reflector_queue")
    assert len(writes) == 1 and writes[0][1].startswith("UPDATE") and "llm down" in writes[0][2][0], log


def test_reflector_supersedes_through_the_shared_owner(each_backend, monkeypatch):
    import run_reflector
    from memory.backends import dialect
    from memory.db import _db

    tag = uuid.uuid4().hex
    old = _write(f"User lives in Seattle {tag}")
    new = _write(f"User moved to Austin {tag}")
    rows = [(old, {"text": f"User lives in Seattle {tag}"}), (new, {"text": f"User moved to Austin {tag}"})]
    monkeypatch.setattr(run_reflector, "_load_observations_for_conv", lambda *_a, **_k: rows)

    async def reflector(*_a, **_k):
        return [], [{"new_text": rows[1][1]["text"], "old_text": rows[0][1]["text"]}]

    monkeypatch.setattr(run_reflector, "call_reflector", reflector)
    counters = {"processed": 0, "sup_emitted": 0, "sup_written": 0, "failed": 0, "empty_groups": 0}
    res = asyncio.run(run_reflector.reflect_conversation("u", "c", None, None, "t", counters))
    assert res == {"outcome": "written", "error": None, "superseded": 1}, res
    with _db() as db:
        r = db.execute(f"SELECT is_deleted FROM memory_items WHERE id = {dialect().param()}", (old,)).fetchone()
    assert r[0] == 1, "the superseded observation's validity must be closed"
    again = asyncio.run(run_reflector.reflect_conversation("u", "c", None, None, "t", counters))
    assert again["superseded"] == 0 and counters["sup_already_closed"] == 1, (again, counters)


def test_extract_entities_preview_reports_a_fallback(monkeypatch):
    ent = importlib.import_module("memory.entity")
    ext_mod = importlib.import_module("memory.extraction")

    class _Ext:
        async def extract(self, text):
            return {"entities": [], "relationships": [], ent.EXTRACTOR_FALLBACK_KEY: "LLM extractor failed"}

    monkeypatch.setattr(ext_mod, "get_configured_extractor", lambda: _Ext())
    out = json.loads(asyncio.run(ext_mod.extract_entities_impl("Alice met Bob")))
    assert out[ent.EXTRACTOR_FALLBACK_KEY] == "LLM extractor failed"
