"""chatlog_list_conversations runs on every backend.

It selected the per-turn host_agent and model_id without aggregating them in a
GROUP BY conversation_id: SQLite returns any row's value, PostgreSQL raises
GroupingError, so the tool failed on every PostgreSQL call.
"""

from __future__ import annotations

import json
import uuid


def test_lists_a_conversation_with_its_counts_and_agent(each_backend):
    import chatlog_core
    from m3_sdk import M3Context
    from memory.backends import dialect
    from memory.backends.dialect import chatlog_table

    d = dialect()
    p = d.param()
    cid = f"lc{uuid.uuid4().hex[:8]}"
    table = chatlog_table("items")
    meta = d.json_bind_value({"host_agent": "codex", "model_id": "gpt-5"})
    with M3Context.for_db(None).get_chatlog_conn() as conn:
        for ts in ("2026-10-18T10:00:00Z", "2026-10-18T11:00:00Z"):
            conn.execute(
                f"INSERT INTO {table} (id, type, content, metadata_json, conversation_id, created_at) "
                f"VALUES ({d.placeholder(6)})",
                (str(uuid.uuid4()), "chat_log", "x", meta, cid, ts),
            )
        conn.commit()
    try:
        out = json.loads(chatlog_core.chatlog_list_conversations_impl(host_agent="codex", limit=1000))
        row = next((c for c in out["conversations"] if c["conversation_id"] == cid), None)
        assert row is not None, f"observed: {cid} not listed on {each_backend.name}"
        assert row["turns"] == 2 and row["host_agent"] == "codex" and row["model_id"] == "gpt-5", row
    finally:
        with M3Context.for_db(None).get_chatlog_conn() as conn:
            conn.execute(f"DELETE FROM {table} WHERE conversation_id = {p}", (cid,))
            conn.commit()
