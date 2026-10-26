"""One loop embed pass, run end to end on every supported store layout.

Hazard: a step written for one store's schema runs on every store the loop
sweeps. A per-step test that seeds only the store the step was written for stays
green on a layout where the step fails (on SQLite the chatlog file uses the core
table names, so a main-store step reaches it). This test asserts outcomes only:
every pending row in every store is embedded and the loop logs no warning or
error, whatever step a future change adds.

Layouts: SQLite split (main and chatlog files), SQLite unified (one file), and
PostgreSQL (both stores in one database; skips without a reachable cluster).
Stores are built by the real migrations.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from m3_core.paths import pin_chatlog_db, pin_database

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

_DIM = 1024


def _no_embedder_network(monkeypatch):
    """A fake embedder, and no probe of a real embed server on this machine."""
    import memory_core as mc

    async def fake_embed_many(texts):
        return [([0.01] * _DIM, "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    embed = importlib.import_module("memory.embed")
    monkeypatch.setattr(embed, "recover_if_fallback_healthy", lambda: False)


def _sqlite_insert(path: Path, mid: str, kind: str) -> None:
    with sqlite3.connect(str(path)) as c:
        c.execute(
            "INSERT INTO memory_items (id, type, title, content, metadata_json, is_deleted, created_at, user_id, scope) "
            "VALUES (?, ?, ?, ?, '{}', 0, '2026-10-25T00:00:00.000000Z', '', 'agent')",
            (mid, kind, "t", f"a pending {kind} row"),
        )


def _sqlite_embedded(path: Path, mid: str) -> int:
    with sqlite3.connect(str(path)) as c:
        return c.execute("SELECT count(*) FROM memory_embeddings WHERE memory_id = ?", (mid,)).fetchone()[0]


async def _one_pass(database: str | None, caplog) -> list[str]:
    import m3_cognitive_loop as loop

    caplog.set_level(logging.WARNING, logger="m3_cognitive_loop")
    await loop.run_embed_pass(argparse.Namespace(database=database, limit_per_pass=100_000))
    return [f"{r.levelname}: {r.getMessage()}" for r in caplog.records if r.name == "m3_cognitive_loop"]


@pytest.mark.asyncio
async def test_one_embed_pass_on_split_sqlite_embeds_everything_and_logs_nothing(tmp_path, monkeypatch, caplog):
    from conftest import create_full_chatlog_schema, create_full_main_schema

    main, chat = tmp_path / "agent_memory.db", tmp_path / "agent_chatlog.db"
    create_full_main_schema(main)
    create_full_chatlog_schema(chat)
    pin_database(str(main))
    pin_chatlog_db(str(chat))
    _no_embedder_network(monkeypatch)
    memory_row, chat_row = str(uuid.uuid4()), str(uuid.uuid4())
    _sqlite_insert(main, memory_row, "note")
    _sqlite_insert(chat, chat_row, "chat_log")

    logged = await _one_pass(str(main), caplog)

    assert logged == [], f"observed: the loop logged on split SQLite: {logged}"
    assert _sqlite_embedded(main, memory_row) == 1, "observed: the main store's row was not embedded"
    assert _sqlite_embedded(chat, chat_row) == 1, "observed: the chatlog file's row was not embedded"


@pytest.mark.asyncio
async def test_a_pending_row_in_the_chatlog_file_alone_is_embedded(tmp_path, monkeypatch, caplog):
    """The pass's work check must look at every store: with the main store clean,
    a check of the main store alone reports no work and the chat row waits forever."""
    from conftest import create_full_chatlog_schema, create_full_main_schema

    main, chat = tmp_path / "agent_memory.db", tmp_path / "agent_chatlog.db"
    create_full_main_schema(main)
    create_full_chatlog_schema(chat)
    pin_database(str(main))
    pin_chatlog_db(str(chat))
    _no_embedder_network(monkeypatch)
    chat_row = str(uuid.uuid4())
    _sqlite_insert(chat, chat_row, "chat_log")

    logged = await _one_pass(str(main), caplog)

    assert logged == [], f"observed: the loop logged: {logged}"
    assert _sqlite_embedded(chat, chat_row) == 1, "observed: the chat row was not embedded with the main store clean"


@pytest.mark.asyncio
async def test_one_embed_pass_on_unified_sqlite_embeds_everything_and_logs_nothing(tmp_path, monkeypatch, caplog):
    from conftest import create_full_main_schema

    main = tmp_path / "agent_memory.db"
    create_full_main_schema(main)
    pin_database(str(main))
    pin_chatlog_db(str(main))
    _no_embedder_network(monkeypatch)
    memory_row, chat_row = str(uuid.uuid4()), str(uuid.uuid4())
    _sqlite_insert(main, memory_row, "note")
    _sqlite_insert(main, chat_row, "chat_log")

    logged = await _one_pass(str(main), caplog)

    assert logged == [], f"observed: the loop logged on unified SQLite: {logged}"
    assert _sqlite_embedded(main, memory_row) == 1, "observed: the memory row was not embedded"
    assert _sqlite_embedded(main, chat_row) == 1, "observed: the chat row was not embedded"


@pytest.mark.requires_pg
@pytest.mark.asyncio
async def test_one_embed_pass_on_postgres_embeds_everything_and_logs_nothing(monkeypatch, caplog):
    """Both stores in one database; the test database is shared, so only the
    rows inserted here are asserted on."""
    from conftest import pg_dsn, use_backend

    use_backend("postgres", pg_dsn())
    selector = importlib.import_module("memory.backends.selector")
    selector._reset_for_tests()
    try:
        backends = importlib.import_module("memory.backends")
        b = backends.active_backend()
        assert b.name == "postgres"
        b.ensure_schema()
        _no_embedder_network(monkeypatch)
        rows = {"memory_items": str(uuid.uuid4()), "chat_log_items": str(uuid.uuid4())}
        with b.connection() as c:
            cur = c.cursor()
            for table, mid in rows.items():
                cur.execute(
                    f"INSERT INTO {table} (id, type, title, content, importance, created_at) "
                    "VALUES (%s, %s, 't', %s, 0.5, now())",
                    (mid, "chat_log" if table == "chat_log_items" else "note", f"a pending row in {table}"),
                )
            c.commit()

        logged = await _one_pass(None, caplog)

        # The database is shared, so the pass also sweeps rows other tests left.
        assert logged == [], (
            f"observed: the loop logged on PostgreSQL: {logged}. inspect: whether the message names a "
            f"row inserted here or one another test left in the shared database"
        )
        embeddings = {"memory_items": "memory_embeddings", "chat_log_items": "chat_log_embeddings"}
        with b.connection() as c:
            cur = c.cursor()
            for table, mid in rows.items():
                cur.execute(f"SELECT count(*) FROM {embeddings[table]} WHERE memory_id = %s", (mid,))
                assert cur.fetchone()[0] == 1, f"observed: the {table} row was not embedded on PostgreSQL"
    finally:
        selector._reset_for_tests()
