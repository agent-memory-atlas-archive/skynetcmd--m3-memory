"""The LangChain client's chatlog delete must hit the chatlog rows on every backend.

`M3Client._delete_chatlog_rows_async` soft-deletes on the chatlog connection.
It hardcoded both the SQL dialect ('?' placeholders) and the SQLite table name
(`memory_items`). On PostgreSQL the chatlog lives in `chat_log_items` on the
main pool, so the statement failed on '?', and even with portable placeholders
it would have updated the MAIN store's `memory_items` instead. The method does
not touch `self`, so it is called unbound; LangChain itself is not needed.
"""

from __future__ import annotations

import asyncio
import uuid


def test_delete_marks_the_chatlog_row_deleted(monkeypatch, tmp_path, each_backend):
    import chatlog_core
    from m3_sdk import M3Context
    from memory.backends import dialect
    from memory.backends.dialect import chatlog_table

    from conftest import create_full_main_schema, isolate_chatlog_env
    from m3_memory.integrations.langchain.m3client import M3Client

    if each_backend.name == "sqlite":
        paths = isolate_chatlog_env(monkeypatch, tmp_path)
        create_full_main_schema(paths["db_path"])

    async def _go():
        rid = await chatlog_core.chatlog_write_impl(
            content="langchain delete probe",
            role="user",
            conversation_id=f"c-{uuid.uuid4()}",
            host_agent="langchain",
            provider="other",
            model_id="test",
        )
        await chatlog_core._flush_once()
        n = await M3Client._delete_chatlog_rows_async(None, [rid])
        return rid, n

    rid, n = asyncio.run(_go())
    assert n == 1, f"delete affected {n} rows"
    d = dialect()
    with M3Context.for_db(None).get_chatlog_conn() as conn:
        row = conn.execute(f"SELECT is_deleted FROM {chatlog_table('items')} WHERE id = {d.param()}", (rid,)).fetchone()
    assert row is not None and row[0] == 1, f"chatlog row {rid} not soft-deleted: {row}"
