"""A never-written SQLite chatlog store gets its schema from get_chatlog_conn().

Measured on a fresh install: chatlog_search on a chatlog file nobody had written
raised `sqlite3.OperationalError: no such table: memory_items_fts`; writes worked
only because some integrations ran their own copy of a schema self-heal. The
owner is now M3Context.get_chatlog_conn() -> memory.db.ensure_chatlog_schema.
"""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

from conftest import isolate_chatlog_env


@pytest.fixture
def fresh_chatlog(tmp_path, monkeypatch):
    paths = isolate_chatlog_env(monkeypatch, tmp_path)
    db = importlib.import_module("memory.db")
    monkeypatch.setattr(db, "_initialized_chatlog_dbs", set())
    assert not paths["db_path"].exists(), "precondition: the chatlog store has never been written"
    return paths


@pytest.mark.asyncio
async def test_fresh_store_search_is_empty_then_a_write_is_found(fresh_chatlog):
    import chatlog_core

    out = await chatlog_core.chatlog_search_impl("anything at all")
    assert "no such table" not in out
    parsed = json.loads(out)
    hits = parsed if isinstance(parsed, list) else parsed.get("results", parsed.get("hits", []))
    assert hits == [] or hits == {}, out

    res = await chatlog_core.chatlog_write_bulk_impl(
        [
            {
                "content": "the zebra-crossing phrase is unique",
                "role": "user",
                "conversation_id": "c1",
                "host_agent": "claude-code",
                "provider": "anthropic",
                "model_id": "m",
            }
        ]
    )
    assert res["failed"] == 0, res
    await chatlog_core._flush_once()
    found = await chatlog_core.chatlog_search_impl("zebra")
    assert "zebra-crossing" in found, found


def test_schema_is_ensured_once_per_store_with_captured_output(fresh_chatlog, monkeypatch, capsys):
    db = importlib.import_module("memory.db")
    calls = []
    real = db._ensure_sync_tables

    def _spy(path=None, target=None):
        calls.append((path, target))
        return real(path, target)

    monkeypatch.setattr(db, "_ensure_sync_tables", _spy)
    from m3_core.context import M3Context

    ctx = M3Context(str(fresh_chatlog["db_path"].parent / "agent_memory.db"))
    for _ in range(3):
        with ctx.get_chatlog_conn() as conn:
            conn.execute("SELECT count(*) FROM memory_items_fts").fetchone()
    assert calls == [(str(fresh_chatlog["db_path"]), "chatlog")], calls
    assert capsys.readouterr().out == "", "the migrator must never write to the host's stdout"
    with sqlite3.connect(fresh_chatlog["db_path"]) as c:
        assert c.execute("SELECT name FROM sqlite_master WHERE name = 'memory_items'").fetchone()


def test_flush_scope_still_gets_the_schema(fresh_chatlog):
    """The flush runs under active_database(<chatlog path>), so a context there
    has db_path == the chatlog store. Comparing the chatlog with self.db_path read
    "same file" and skipped the schema: Hermes' first synced turn spilled with
    "no such table: memory_items". Topology is chatlog_config.chatlog_is_separate."""
    import chatlog_config
    from m3_core.context import M3Context
    from m3_core.paths import active_database

    chat = str(fresh_chatlog["db_path"])
    with active_database(chat):
        assert chatlog_config.chatlog_is_separate(chat)
        with M3Context(chat).get_chatlog_conn() as conn:
            conn.execute("SELECT count(*) FROM memory_items").fetchone()


@pytest.mark.asyncio
async def test_fresh_store_first_write_lands(fresh_chatlog):
    import chatlog_core

    res = await chatlog_core.chatlog_write_bulk_impl(
        [
            {
                "content": "a first-ever okapi turn",
                "role": "user",
                "conversation_id": "c2",
                "host_agent": "hermes",
                "provider": "other",
                "model_id": "m",
            }
        ]
    )
    assert res["failed"] == 0, res
    await chatlog_core._flush_once()
    assert "okapi" in await chatlog_core.chatlog_search_impl("okapi")
