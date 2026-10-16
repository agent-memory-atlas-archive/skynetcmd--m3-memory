"""chatlog_ingest reports and cursors only what is DURABLE.

Real operations against temp roots: a transcript is ingested, the rows are
counted in the (temp) chatlog DB, the cursor is read back. Failure paths break
the real DB write / spill write and assert the cursor did not move and the
exit code is non-zero, then repair and assert the turns land exactly once.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from conftest import create_full_chatlog_schema, isolate_chatlog_env

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "claude_code_sample.jsonl")
SESSION = "fix-claude-0001"


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = isolate_chatlog_env(monkeypatch, tmp_path)
    cursor = tmp_path / ".chatlog_ingest_cursor.json"
    import chatlog_ingest

    monkeypatch.setattr(chatlog_ingest, "_cursor_path", lambda: str(cursor))
    create_full_chatlog_schema(paths["db_path"])
    return {"db": paths["db_path"], "cursor": cursor, "spill": tmp_path / "chatlog_spill"}


def _rows(db) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE type='chat_log' AND conversation_id=?", (SESSION,)
        ).fetchone()[0]
    finally:
        conn.close()


def _seen(cursor) -> list:
    if not cursor.exists():
        return []
    return json.loads(cursor.read_text(encoding="utf-8")).get("sessions", {}).get(SESSION, {}).get("seen_uuids", [])


def _spill_lines(spill_dir) -> int:
    if not spill_dir.exists():
        return 0
    return sum(len(p.read_text(encoding="utf-8").splitlines()) for p in spill_dir.rglob("*.jsonl"))


@pytest.mark.asyncio
async def test_ingest_lands_every_turn_exactly_once_before_reporting(env):
    import chatlog_ingest

    first = await chatlog_ingest._ingest("claude-code", FIXTURE, "", "test")
    # No extra flush: _ingest drained the queue itself before reporting.
    assert first["written"] == 4 and first["pending"] == 0 and first["spilled"] == 0, first
    assert _rows(env["db"]) == 4
    assert len(_seen(env["cursor"])) == 4
    assert chatlog_ingest._exit_code(first) == 0

    second = await chatlog_ingest._ingest("claude-code", FIXTURE, "", "test")
    assert second["written"] == 0 and second["skipped"] == 4
    assert _rows(env["db"]) == 4, "re-ingest duplicated rows"


@pytest.mark.asyncio
async def test_db_failure_spills_and_the_spill_is_cursored(env, monkeypatch):
    import chatlog_core
    import chatlog_ingest

    def _down(batch):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(chatlog_core, "_executemany_insert", _down)
    res = await chatlog_ingest._ingest("claude-code", FIXTURE, "", "test")
    assert res["written"] == 0 and res["spilled"] == 4 and res["pending"] == 0, res
    assert res["spill_dir"] == str(env["spill"])
    assert _spill_lines(env["spill"]) == 4, "spilled rows are not on disk"
    assert len(_seen(env["cursor"])) == 4, "durable (spilled) turns must be cursored"
    assert _rows(env["db"]) == 0
    assert chatlog_ingest._exit_code(res) == 0


@pytest.mark.asyncio
async def test_db_and_spill_failure_reports_pending_and_keeps_the_cursor(env, monkeypatch):
    import chatlog_core
    import chatlog_ingest

    real_insert = chatlog_core._executemany_insert

    def _down(batch):
        raise sqlite3.OperationalError("disk I/O error")

    def _spill_down(batch):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(chatlog_core, "_executemany_insert", _down)
    monkeypatch.setattr(chatlog_core, "_spill_batch", _spill_down)
    res = await chatlog_ingest._ingest("claude-code", FIXTURE, "", "test")
    assert res["written"] == 0 and res["pending"] == 4, res
    assert "neither reached the chatlog DB nor a spill file" in res["error"]
    assert _seen(env["cursor"]) == [], "cursor advanced for rows that are not durable"
    assert chatlog_ingest._exit_code(res) == 1

    # Repair: the next fire retries the same turns and they land exactly once.
    monkeypatch.setattr(chatlog_core, "_executemany_insert", real_insert)
    again = await chatlog_ingest._ingest("claude-code", FIXTURE, "", "test")
    assert again["written"] == 4 and again["pending"] == 0, again
    assert _rows(env["db"]) == 4
    assert len(_seen(env["cursor"])) == 4


@pytest.mark.asyncio
async def test_corrupt_cursor_refuses_and_is_moved_aside(env, caplog):
    import logging

    import chatlog_ingest

    env["cursor"].write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="chatlog_ingest"):
        res = await chatlog_ingest._ingest("claude-code", FIXTURE, "", "test")
    assert res["written"] == 0 and "corrupt" in res["error"], res
    assert _rows(env["db"]) == 0, "ingested on a corrupt cursor (would duplicate every turn)"
    aside = list(env["cursor"].parent.glob(env["cursor"].name + ".corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == "{not json"
    assert str(aside[0]) in res["error"]
    assert not env["cursor"].exists()
    assert chatlog_ingest._exit_code(res) == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_a_non_object_cursor_is_refused_too(env):
    import chatlog_ingest

    env["cursor"].write_text("[]", encoding="utf-8")
    with pytest.raises(chatlog_ingest.CursorUnreadable):
        chatlog_ingest._load_cursor()


def test_a_held_cursor_lock_serialises_ingest(env):
    """While another process holds the cursor lock, ingest neither writes rows
    nor touches the cursor; once released it completes. This is what stops two
    concurrent hook fires from double-writing and losing each other's cursor."""
    import chatlog_ingest
    from m3_core.locking import file_lock

    result: dict = {}

    def _run():
        result.update(asyncio.run(chatlog_ingest._ingest("claude-code", FIXTURE, "", "test")))

    with file_lock(chatlog_ingest._cursor_lock_path(), purpose="test holder"):
        t = threading.Thread(target=_run)
        t.start()
        time.sleep(0.8)
        assert t.is_alive(), "ingest did not wait for the cursor lock"
        assert _rows(env["db"]) == 0 and not env["cursor"].exists()
    t.join(timeout=60)
    assert not t.is_alive()
    assert result["written"] == 4 and _rows(env["db"]) == 4


def test_cli_exits_nonzero_when_it_reports_an_error(tmp_path):
    """The .sh wrappers exec this; exit 0 on an error made every failure silent."""
    env = dict(os.environ)
    env.update(M3_ENGINE_ROOT=str(tmp_path / "engine"), M3_CONFIG_ROOT=str(tmp_path / "config"))
    script = os.path.join(HERE, "..", "bin", "chatlog_ingest.py")
    proc = subprocess.run(
        [sys.executable, script, "--format", "claude-code", "--transcript-path", str(tmp_path / "missing.jsonl")],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-400:])
    assert "transcript not found" in proc.stdout


def test_exit_code_contract():
    import chatlog_ingest

    assert chatlog_ingest._exit_code({"written": 1}) == 0
    assert chatlog_ingest._exit_code({"spilled": 2}) == 0
    assert chatlog_ingest._exit_code({"error": "x"}) == 1
    assert chatlog_ingest._exit_code({"failed": 1}) == 1
    assert chatlog_ingest._exit_code({"pending": 1}) == 1


def test_aider_header_stamp_is_stored_as_utc():
    from datetime import UTC, datetime

    import chatlog_ingest

    stamp = "2026-01-11 11:00:00"
    expected = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").astimezone().astimezone(UTC)
    items, sid = chatlog_ingest._parse_aider(f"# aider chat started at {stamp}\n#### q\n\nan answer\n")
    assert sid == stamp, "the raw stamp stays the conversation identity"
    assert items[0]["timestamp"] == expected.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert chatlog_ingest._aider_stamp_to_utc("not a stamp") == ""
