"""A chat turn with no visible text is not stored, and capture treats it as handled.

Hazard: a turn holding only harness framing (a <system-reminder>, a task
notification) is empty once capture strips it. Stored, it is a blank row; left
uncursored, every hook fire retries it and counts it as a failure.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import create_full_chatlog_schema, isolate_chatlog_env

FRAMING_ONLY = "<system-reminder>background task finished</system-reminder>"


@pytest.fixture
def ingest_env(tmp_path, monkeypatch):
    paths = isolate_chatlog_env(monkeypatch, tmp_path)
    import chatlog_ingest

    cursor_file = tmp_path / ".chatlog_ingest_cursor.json"
    monkeypatch.setattr(chatlog_ingest, "_cursor_path", lambda: str(cursor_file))
    create_full_chatlog_schema(paths["db_path"])
    return {"db": paths["db_path"], "tmp": tmp_path}


def _turn(**overrides):
    item = {
        "role": "user",
        "conversation_id": "c-blank",
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-opus-5-5",
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize("content", [FRAMING_ONLY, "   \n\t", "\u200b\u202e", FRAMING_ONLY + " \u200b "])
@pytest.mark.asyncio
async def test_a_single_blank_write_is_refused(ingest_env, content):
    import chatlog_core

    with pytest.raises(ValueError, match="no visible text"):
        await chatlog_core.chatlog_write_impl(content=content, **_turn())


@pytest.mark.asyncio
async def test_bulk_reports_blank_turns_apart_from_failures(ingest_env):
    import chatlog_core

    res = await chatlog_core.chatlog_write_bulk_impl(
        [_turn(content=FRAMING_ONLY), _turn(content="a real question", turn_index=1)]
    )
    assert res["blank"] == 1 and res["failed"] == 0 and len(res["written_ids"]) == 1, res


@pytest.mark.asyncio
async def test_ingest_cursors_a_framing_only_turn_and_stores_the_rest(ingest_env):
    import chatlog_ingest

    sid = "s-blank"
    transcript = ingest_env["tmp"] / "t.jsonl"
    rows = [
        {
            "type": "user",
            "message": {"role": "user", "content": FRAMING_ONLY},
            "uuid": "u-1",
            "timestamp": "2026-10-18T17:00:00.000Z",
            "sessionId": sid,
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "deploy the site"},
            "uuid": "u-2",
            "timestamp": "2026-10-18T17:00:01.000Z",
            "sessionId": sid,
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-opus-5-5",
                "role": "assistant",
                "content": [{"type": "text", "text": "Deployed."}],
            },
            "uuid": "a-1",
            "timestamp": "2026-10-18T17:00:05.000Z",
            "sessionId": sid,
        },
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    first = await chatlog_ingest._ingest("claude-code", str(transcript), session_override="", variant="test")
    assert first["failed"] == 0 and first["written"] == 2 and first["skipped"] == 1, first

    again = await chatlog_ingest._ingest("claude-code", str(transcript), session_override="", variant="test")
    assert again["failed"] == 0 and again["written"] == 0 and again["skipped"] == 3, (
        f"the blank turn was not cursored, so every hook fire would retry it: {again}"
    )

    conn = sqlite3.connect(ingest_env["db"])
    try:
        stored = [r[0] for r in conn.execute("SELECT content FROM memory_items WHERE type='chat_log'")]
    finally:
        conn.close()
    assert sorted(stored) == ["Deployed.", "deploy the site"], stored
