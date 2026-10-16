"""The cognitive loop's embed pass must actually drain chatlog spill.

`drain_spill` awaits `asyncio.to_thread`. The loop once drove it with
`coro.send(None)` on a worker thread, which raises on that await; the error was
caught per spill target, each file was marked failed, and after
`max_drain_attempts` it was quarantined — spilled turns never landed and the
recovery path quietly moved them aside. These tests run the real drain through
`_drain_chatlog_spill` against a temp store.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from m3_core.paths import pin_chatlog_db, pin_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _spill_one(spill_dir: Path, marker: str) -> str:
    spill_dir.mkdir(parents=True, exist_ok=True)
    rid = str(uuid.uuid4())
    line = {
        "_id": rid,
        "_content": f"{marker} spilled turn",
        "_title": "loop-spill-drain",
        "role": "user",
        "conversation_id": f"{marker}-conv",
        "host_agent": "pytest",
        "_created_at": "2026-10-16T00:00:00Z",
    }
    (spill_dir / "20261016.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    return rid


def _count(rid: str) -> int:
    from m3_sdk import M3Context
    from memory.backends import chatlog_table, dialect

    with M3Context.for_db(None).get_chatlog_conn() as conn:
        cur = conn.execute(
            f"SELECT COUNT(*) FROM {chatlog_table('items')} WHERE id = {dialect().param()}",
            (rid,),
        )
        return int(cur.fetchone()[0])


def _setup_store(tmp_path, monkeypatch) -> Path:
    from conftest import create_full_main_schema

    db = tmp_path / "chatlog.db"
    create_full_main_schema(str(db))
    pin_database(str(db))
    pin_chatlog_db(str(db))

    import chatlog_config

    spill_dir = tmp_path / "spill"
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))
    return spill_dir


def test_loop_drains_a_real_spill_line_exactly_once(tmp_path, monkeypatch):
    spill_dir = _setup_store(tmp_path, monkeypatch)
    marker = f"loop-{uuid.uuid4().hex[:10]}"
    rid = _spill_one(spill_dir, marker)

    import m3_cognitive_loop as L

    assert _count(rid) == 0
    drained = asyncio.run(L._drain_chatlog_spill())
    assert drained == 1, "the loop's drain reported no rows — it did not run drain_spill to completion"
    assert _count(rid) == 1

    # Nothing left to drain, nothing quarantined, and a second pass is a no-op.
    assert not list(spill_dir.glob("*.jsonl"))
    quarantine = spill_dir / "quarantine"
    assert not (quarantine.exists() and any(quarantine.iterdir())), "drained spill was quarantined"
    assert asyncio.run(L._drain_chatlog_spill()) == 0
    assert _count(rid) == 1


def test_loop_drain_failure_is_logged_and_never_raises(tmp_path, monkeypatch, caplog):
    spill_dir = _setup_store(tmp_path, monkeypatch)
    _spill_one(spill_dir, f"loop-{uuid.uuid4().hex[:10]}")

    import chatlog_embed_sweeper
    import m3_cognitive_loop as L

    async def _boom(conn=None):
        raise OSError("simulated spill target failure")

    monkeypatch.setattr(chatlog_embed_sweeper, "drain_spill", _boom)
    with caplog.at_level("WARNING"):
        assert asyncio.run(L._drain_chatlog_spill()) == 0
    assert "simulated spill target failure" in caplog.text
    # The spill file is untouched by a failed drain.
    assert list(spill_dir.glob("*.jsonl"))


def test_has_spill_work_counts_pending_not_quarantine_and_warns(tmp_path, monkeypatch, caplog):
    import chatlog_config
    import m3_cognitive_loop as L

    spill_dir = tmp_path / "spill"
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))
    monkeypatch.setattr(L, "_QUARANTINE_WARNED_AT", 0.0)
    q = spill_dir / "quarantine"
    q.mkdir(parents=True)
    (q / "20261001.jsonl").write_text("{}\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger="m3_cognitive_loop"):
        assert L.has_spill_work() is False  # quarantined files are not drain work
        assert L.has_spill_work() is False
    hits = [r.getMessage() for r in caplog.records if "quarantined chatlog spill" in r.getMessage()]
    assert len(hits) == 1, "warning must be rate-limited"
    assert str(q) in hits[0] and str(spill_dir) in hits[0]

    (spill_dir / "20261016.jsonl").write_text("{}\n", encoding="utf-8")
    assert L.has_spill_work() is True
