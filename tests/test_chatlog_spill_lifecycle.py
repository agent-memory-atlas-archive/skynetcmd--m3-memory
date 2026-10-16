"""Spill -> drain -> store, end to end, against a temp store.

Every test performs the real operation: the shipped writer (`_spill_batch`)
puts a turn on disk, the shipped drain (`drain_spill`) moves it, and the store
is counted. A turn must land exactly once, or be parked intact under
quarantine/ — never deleted unread, never retried forever.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import uuid
from pathlib import Path

import pytest
from m3_core.paths import pin_chatlog_db, pin_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _store(tmp_path, monkeypatch) -> tuple[Path, Path]:
    from conftest import create_full_main_schema

    db = tmp_path / "chatlog.db"
    create_full_main_schema(str(db))
    pin_database(str(db))
    pin_chatlog_db(str(db))  # cleared after the test by conftest

    import chatlog_config

    spill_dir = tmp_path / "spill"
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))
    monkeypatch.setattr(chatlog_config, "STATE_FILE", str(tmp_path / ".chatlog_state.json"))
    chatlog_config.invalidate_cache()
    return db, spill_dir


def _item(marker: str, db_path: str | None = None, rid: str | None = None) -> dict:
    return {
        "_id": rid or str(uuid.uuid4()),
        "_title": "spill-lifecycle",
        "_content": f"{marker} turn",
        "_metadata_json": json.dumps({"role": "user", "host_agent": "claude-code"}),
        "_created_at": "2026-10-16T00:00:00Z",
        "_db_path": db_path,
        "model_id": "m",
        "role": "user",
        "host_agent": "claude-code",
        "conversation_id": f"{marker}-conv",
    }


def _count(db: Path, rid: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memory_items WHERE id = ?", (rid,)).fetchone()[0])
    finally:
        conn.close()


def _drain() -> int:
    import chatlog_embed_sweeper

    return asyncio.run(chatlog_embed_sweeper.drain_spill())


def test_spilled_turn_lands_exactly_once(tmp_path, monkeypatch):
    import chatlog_config
    import chatlog_core

    db, spill_dir = _store(tmp_path, monkeypatch)
    it = _item("once", str(db))
    chatlog_core._spill_batch([it])
    assert chatlog_config.spill_inventory()["pending_files"] == 1
    assert chatlog_config.read_state()["spill"]["files"] == 1

    assert _drain() == 1
    assert _count(db, it["_id"]) == 1
    assert not list(spill_dir.glob("*.jsonl"))
    assert _drain() == 0
    assert _count(db, it["_id"]) == 1
    # The drain refreshes the state snapshot the status line reads.
    assert chatlog_config.read_state()["spill"]["files"] == 0


def test_append_after_claim_is_not_deleted_unread(tmp_path, monkeypatch):
    """H3: a line the writer appends after the drain claimed the day file goes
    to a NEW day file, so deleting the claimed file cannot drop it."""
    import chatlog_core
    import chatlog_embed_sweeper

    db, spill_dir = _store(tmp_path, monkeypatch)
    a, b = _item("a", str(db)), _item("b", str(db))
    chatlog_core._spill_batch([a])
    day = next(spill_dir.glob("*.jsonl"))
    claimed = chatlog_embed_sweeper._claim_spill_file(str(day))
    assert claimed and ".draining-" in claimed and not day.exists()

    chatlog_core._spill_batch([b])  # the live writer, after the claim
    assert day.exists(), "the late append must start a fresh day file"

    assert _drain() == 2
    assert _count(db, a["_id"]) == 1 and _count(db, b["_id"]) == 1
    assert not list(spill_dir.glob("*.jsonl"))


def test_partial_target_success_does_not_retry_landed_rows(tmp_path, monkeypatch):
    """H4: one target lands, another fails; the file is rewritten without the
    landed group, and once the failing target is fixed both are present once."""
    from conftest import create_full_main_schema

    db, spill_dir = _store(tmp_path, monkeypatch)
    engine = Path(tmp_path / "engine")
    engine.mkdir(parents=True, exist_ok=True)
    stub = engine / "not_migrated.db"
    sqlite3.connect(str(stub)).close()  # exists, no schema
    good, later = _item("good", str(db)), _item("later", str(stub))

    import chatlog_core

    chatlog_core._spill_batch([good, later])
    assert _drain() == 1
    assert _count(db, good["_id"]) == 1
    pending = list(spill_dir.glob("*.jsonl"))
    assert len(pending) == 1
    kept = [json.loads(x) for x in pending[0].read_text(encoding="utf-8").splitlines()]
    assert [k["_id"] for k in kept] == [later["_id"]], "landed rows must leave the file"

    stub.unlink()
    create_full_main_schema(str(stub))
    assert _drain() == 1
    # Routing of a captured _db_path is the writer's business (an explicit
    # chatlog pin wins over it); what matters here is exactly-once.
    assert _count(stub, later["_id"]) + _count(db, later["_id"]) == 1
    assert _count(db, good["_id"]) == 1
    assert not list(spill_dir.glob("*.jsonl"))


def test_redrain_of_committed_rows_is_idempotent(tmp_path, monkeypatch):
    """H4: a drain that committed and died before removing its file retries
    the same ids; that must succeed (ignored), not fail on the primary key."""
    import chatlog_core

    db, spill_dir = _store(tmp_path, monkeypatch)
    it = _item("dup", str(db))
    chatlog_core._executemany_insert([dict(it)])
    chatlog_core._spill_batch([it])
    assert _drain() == 1
    assert _count(db, it["_id"]) == 1
    assert not list(spill_dir.glob("*.jsonl"))
    assert not (spill_dir / "quarantine").exists()


def test_malformed_line_is_parked_not_deleted(tmp_path, monkeypatch):
    """M40: a line that is not a JSON object goes to quarantine/<file>.bad."""
    db, spill_dir = _store(tmp_path, monkeypatch)
    spill_dir.mkdir(parents=True)
    it = _item("ok", str(db))
    (spill_dir / "20261016.jsonl").write_text(json.dumps(it) + "\n{not json\n[1, 2]\n", encoding="utf-8")

    assert _drain() == 1
    assert _count(db, it["_id"]) == 1
    bad = list((spill_dir / "quarantine").glob("*.bad"))
    assert len(bad) == 1
    assert bad[0].read_text(encoding="utf-8").splitlines() == ["{not json", "[1, 2]"]
    assert not list(spill_dir.glob("*.jsonl"))


def test_malformed_line_kept_when_it_cannot_be_parked(tmp_path, monkeypatch):
    import chatlog_embed_sweeper

    db, spill_dir = _store(tmp_path, monkeypatch)
    spill_dir.mkdir(parents=True)
    it = _item("ok", str(db))
    (spill_dir / "20261016.jsonl").write_text(json.dumps(it) + "\n{not json\n", encoding="utf-8")

    def _no_park(*a, **k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(chatlog_embed_sweeper, "_park_lines", _no_park)
    assert _drain() == 1
    pending = list(spill_dir.glob("*.jsonl"))
    assert len(pending) == 1, "an unparkable line must stay in the spill file"
    assert pending[0].read_text(encoding="utf-8").splitlines() == ["{not json"]


def test_foreign_temp_target_is_parked_and_real_lines_drain(tmp_path, monkeypatch, caplog):
    """A line aimed at a temp-dir store that is not this install's (a test or
    scratch DB) is moved to quarantine/foreign-<file>; the rest drains."""
    import tempfile

    db, spill_dir = _store(tmp_path, monkeypatch)
    # The suite's own temp dir is per-test; make tmp_path "the temp dir" so the
    # engine root and the pinned store sit inside it, as on a real box they don't.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    other = tmp_path / "someone-elses-test"  # under the temp dir, outside the engine root
    other.mkdir()
    real, foreign = _item("real", str(db)), _item("foreign", str(other / "test_agent_chatlog.db"))

    import chatlog_core

    chatlog_core._spill_batch([real, foreign])
    with caplog.at_level("WARNING"):
        assert _drain() == 1
    assert _count(db, real["_id"]) == 1
    assert not (other / "test_agent_chatlog.db").exists(), "drain created the foreign store"
    parked = list((spill_dir / "quarantine").glob("foreign-*"))
    assert len(parked) == 1
    assert [json.loads(x)["_id"] for x in parked[0].read_text(encoding="utf-8").splitlines()] == [foreign["_id"]]
    assert str(other / "test_agent_chatlog.db") in caplog.text and "1 spilled line" in caplog.text
    assert not list(spill_dir.glob("*.jsonl"))


def test_foreign_reason_is_sqlite_only_and_spares_own_store(tmp_path, monkeypatch):
    import chatlog_embed_sweeper as S

    db, _ = _store(tmp_path, monkeypatch)
    (tmp_path / "engine").mkdir(exist_ok=True)
    assert S._foreign_target_reason(str(db), "sqlite") is None
    assert S._foreign_target_reason(str(tmp_path / "engine" / "x.db"), "sqlite") is None
    assert S._foreign_target_reason(str(tmp_path / "gone" / "x.db"), "sqlite")
    assert S._foreign_target_reason("postgresql://h/db", "postgres") is None
    assert S._foreign_target_reason(None, "sqlite") is None


def test_insert_failure_keeps_file_then_quarantines_with_reason(tmp_path, monkeypatch):
    """Failure path: the store refuses; the turn stays on disk, attempts are
    counted, and past the budget the file is quarantined with a reason that
    status reports."""
    import chatlog_config
    import chatlog_core
    import chatlog_embed_sweeper
    import chatlog_status

    db, spill_dir = _store(tmp_path, monkeypatch)
    it = _item("refused", str(db))
    chatlog_core._spill_batch([it])
    monkeypatch.setattr(chatlog_embed_sweeper, "_spill_max_attempts", lambda now=None: 2)

    def _refuse(rows, **kw):
        raise sqlite3.OperationalError("database is locked (simulated)")

    monkeypatch.setattr(chatlog_core, "_executemany_insert", _refuse)
    assert _drain() == 0
    pending = list(spill_dir.glob("*.jsonl"))
    assert len(pending) == 1 and it["_id"] in pending[0].read_text(encoding="utf-8")
    assert _drain() == 0
    assert not list(spill_dir.glob("*.jsonl"))

    inv = chatlog_config.spill_inventory()
    assert inv["quarantine_files"] == 1 and inv["pending_files"] == 0
    reason = next((spill_dir / "quarantine").glob("*.reason.json"))
    assert "database is locked" in json.loads(reason.read_text(encoding="utf-8"))["reason"]
    warnings = chatlog_status._compute_warnings({}, chatlog_config.resolve_config(), {}, 5, 15, spill=inv)
    assert any("quarantined spill" in w and str(spill_dir / "quarantine") in w for w in warnings), warnings
    assert chatlog_config.read_state()["spill"]["quarantine_files"] == 1


def test_committed_flush_with_state_failure_is_not_respilled(tmp_path, monkeypatch):
    """H5: rows committed, state file write fails -> no spill, no duplicate."""
    import chatlog_core

    db, spill_dir = _store(tmp_path, monkeypatch)
    it = _item("h5", str(db))

    def _state_fails(*a):
        raise OSError("state file read-only (simulated)")

    monkeypatch.setattr(chatlog_core, "_update_state_after_flush", _state_fails)

    async def _run():
        monkeypatch.setattr(chatlog_core, "_QUEUE", asyncio.Queue())
        chatlog_core._QUEUE.put_nowait(it)
        return await chatlog_core._flush_once()

    assert asyncio.run(_run()) == 1
    assert _count(db, it["_id"]) == 1
    assert not spill_dir.exists() or not list(spill_dir.glob("*.jsonl"))


def test_shutdown_drain_flushes_every_batch(tmp_path, monkeypatch):
    """M39: shutdown drains until empty, not one queue_flush_rows batch."""
    import chatlog_config
    import chatlog_core

    db, _ = _store(tmp_path, monkeypatch)
    cfg = chatlog_config.resolve_config()
    cfg.queue_flush_rows = 2
    monkeypatch.setattr(chatlog_config, "resolve_config", lambda: cfg)
    items = [_item(f"m39-{i}", str(db)) for i in range(5)]

    async def _run():
        monkeypatch.setattr(chatlog_core, "_QUEUE", asyncio.Queue())
        for it in items:
            chatlog_core._QUEUE.put_nowait(it)
        await chatlog_core._drain_all("test")
        return chatlog_core._QUEUE.qsize()

    assert asyncio.run(_run()) == 0
    assert all(_count(db, it["_id"]) == 1 for it in items)


def test_shutdown_drain_reports_leftovers(tmp_path, monkeypatch, caplog):
    import chatlog_core

    _store(tmp_path, monkeypatch)

    async def _stuck():
        return 0  # no progress

    monkeypatch.setattr(chatlog_core, "_flush_once", _stuck)

    async def _run():
        monkeypatch.setattr(chatlog_core, "_QUEUE", asyncio.Queue())
        chatlog_core._QUEUE.put_nowait({"_id": "x"})
        with caplog.at_level("ERROR"):
            await chatlog_core._drain_all("test")

    asyncio.run(_run())
    assert "1 queued row(s)" in caplog.text and "spill dir" in caplog.text


def test_state_updates_from_concurrent_writers_are_not_lost(tmp_path, monkeypatch):
    """M38: locked read-modify-write; unlocked, increments overwrite each other."""
    import chatlog_config

    _store(tmp_path, monkeypatch)

    def _bump(key):
        for _ in range(15):
            chatlog_config.update_state(lambda s, k=key: s.__setitem__(k, int(s.get(k, 0)) + 1))

    threads = [threading.Thread(target=_bump, args=(f"k{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    state = chatlog_config.read_state()
    assert [state.get(f"k{i}") for i in range(3)] == [15, 15, 15]


def test_redaction_compile_errors_reach_status(tmp_path, monkeypatch):
    """H14: a bad custom regex is persisted where status reads it."""
    import chatlog_config
    import chatlog_core
    import chatlog_redaction
    import chatlog_status

    _store(tmp_path, monkeypatch)
    monkeypatch.setattr(chatlog_core, "_REDACTION_ERRORS_SEEN", None)
    cfg = {"enabled": True, "patterns": ["custom_regex"], "custom_regex": ["[unclosed"], "redact_pii": False}
    chatlog_redaction.scrub("irrelevant", cfg)
    chatlog_core._sync_redaction_errors()
    state = chatlog_config.read_state()
    assert state["redaction"]["regex_errors"], state

    live = chatlog_config.resolve_config()
    live.redaction.enabled = True
    warnings = chatlog_status._compute_warnings(state, live, {}, 5, 15)
    assert any("redaction regex compilation failed" in w for w in warnings)


def test_bulk_validation_errors_name_the_item(tmp_path, monkeypatch):
    """M20: which item failed, without echoing its content."""
    import chatlog_core

    _store(tmp_path, monkeypatch)
    bad = {"content": "SECRET-CONTENT", "role": "nope", "conversation_id": "c1", "turn_index": 7}

    async def _run():
        monkeypatch.setattr(chatlog_core, "_QUEUE", None)
        monkeypatch.setattr(chatlog_core, "_FLUSH_TASK", None)
        res = await chatlog_core.chatlog_write_bulk_impl([bad])
        if chatlog_core._FLUSH_TASK:
            chatlog_core._FLUSH_TASK.cancel()
        return res

    res = asyncio.run(_run())
    assert res["failed"] == 1
    err = res["errors"][0]
    assert err.startswith("item 0") and "'c1'" in err and "7" in err
    assert "SECRET-CONTENT" not in err


def test_unembed_count_failure_is_unknown_not_zero():
    """M37: a failed backlog count is None, never a reassuring 0."""
    import chatlog_embed_sweeper

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memory_items (id TEXT)")
    conn.execute("CREATE TABLE memory_embeddings (memory_id TEXT)")
    assert asyncio.run(chatlog_embed_sweeper.get_unembed_count(conn)) is None


def test_status_reads_the_queue_depth_the_writer_records(tmp_path, monkeypatch):
    """M36: core writes state["queue_depth"]; status must read that key."""
    import chatlog_config
    import chatlog_status

    _store(tmp_path, monkeypatch)
    cfg = chatlog_config.resolve_config()
    state = {"queue_depth": int(cfg.queue_max_depth * 0.9), "last_flush_at": "2026-10-16T00:00:00Z"}
    warnings = chatlog_status._compute_warnings(state, cfg, {}, 5, 15)
    assert any("write queue" in w and "2026-10-16T00:00:00Z" in w for w in warnings), warnings


def test_spill_dir_resolves_from_the_engine_root_at_call_time(tmp_path, monkeypatch):
    """No import-time binding: a root change is seen on the next call."""
    import importlib

    import chatlog_config

    paths = importlib.import_module("m3_core.paths")
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", None)
    root = {"v": str(tmp_path / "e1")}
    monkeypatch.setattr(paths, "get_m3_engine_root", lambda: root["v"])
    assert Path(chatlog_config.spill_dir()) == tmp_path / "e1" / "chatlog_spill"
    root["v"] = str(tmp_path / "e2")
    assert Path(chatlog_config.spill_dir()) == tmp_path / "e2" / "chatlog_spill"


def test_real_spill_guard_can_fail(tmp_path):
    """The conftest guard flags temp-targeted lines and ignores the live writer's.

    The guard's globals are swapped by hand, not monkeypatch: the guard's own
    teardown would otherwise still see the fake dir and fail this test.
    """
    import tempfile

    import conftest

    fake_real = tmp_path / "real_spill"
    fake_real.mkdir()
    saved = conftest._REAL_SPILL_DIRS
    conftest._REAL_SPILL_DIRS = (fake_real,)
    try:
        day = fake_real / "20261016.jsonl"
        day.write_text(json.dumps({"_db_path": "/home/u/.m3/engine/agent_chatlog.db"}) + "\n", encoding="utf-8")
        before = conftest._real_spill_sizes()
        with day.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"_db_path": "/srv/m3/agent_chatlog.db"}) + "\n")  # live writer
        assert conftest._real_spill_offenders(before) == []
        with day.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"_db_path": str(Path(tempfile.gettempdir()) / "pytest-x" / "t.db")}) + "\n")
        offenders = conftest._real_spill_offenders(before)
    finally:
        conftest._REAL_SPILL_DIRS = saved
    assert len(offenders) == 1 and "+1 line" in offenders[0]


@pytest.mark.parametrize("name", ["20261016.jsonl"])
def test_claim_skips_a_file_it_cannot_rename(tmp_path, monkeypatch, name):
    """A day file held open (Windows) or vanished is skipped this pass, not lost."""
    import chatlog_embed_sweeper as S

    _store(tmp_path, monkeypatch)
    assert S._claim_spill_file(str(tmp_path / "spill" / name)) is None
