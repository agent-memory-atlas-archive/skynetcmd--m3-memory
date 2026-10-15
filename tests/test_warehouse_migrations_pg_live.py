"""The warehouse schema comes from its migration chain: applied
automatically by pg_sync, older `public` warehouses migrated in place, one
baseline.

Each test gets a THROWAWAY DATABASE: the shared test database holds the PG
primary's tables in `public`, which the layout probe would take for an older
warehouse.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

import pytest

from conftest import pg_dsn

pytestmark = pytest.mark.requires_pg

ROOT = Path(__file__).resolve().parents[1]

_OLD_PG_SETUP_SHAPE = """
CREATE TABLE memory_items (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT, content TEXT,
    metadata_json JSONB, agent_id TEXT, model_id TEXT, is_deleted INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE
);
INSERT INTO memory_items (id, type, content) VALUES ('m1', 'note', 'kept');
"""


def _with_db(dsn: str, dbname: str) -> str:
    # postgresql://user@host:port/db?params -> same with another database
    return re.sub(r"/[^/?]*(\?|$)", f"/{dbname}\\1", dsn, count=1)


@pytest.fixture
def wh():
    """A fresh, empty warehouse database; dropped afterwards."""
    import psycopg2

    base = pg_dsn()
    name = f"m3_wh_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(_with_db(base, "postgres"))
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    conn = psycopg2.connect(_with_db(base, name))
    try:
        yield conn
    finally:
        conn.close()
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


def _tables(conn, schema):
    cur = conn.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))
    out = {r[0] for r in cur.fetchall()}
    conn.rollback()
    return out


def _cols(conn, schema, table):
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    )
    out = {r[0] for r in cur.fetchall()}
    conn.rollback()
    return out


def test_a_fresh_warehouse_is_created_in_m3_warehouse(wh):
    import pg_sync

    assert pg_sync._prepare_warehouse(wh) == "m3_warehouse"
    assert {"memory_items", "memory_embeddings", "memory_relationships", "tasks", "schema_versions"} <= _tables(
        wh, "m3_warehouse"
    )
    assert {"activity_logs", "project_decisions"} <= _tables(wh, "public"), "the auxiliary tables live in public"

    import migrate_pg

    assert migrate_pg.get_applied_versions(wh, "m3_warehouse") == [1, 2]
    assert migrate_pg.run_warehouse_migrations(wh, "m3_warehouse") == []  # idempotent


def test_an_older_public_warehouse_is_migrated_in_place_and_keeps_its_rows(wh):
    import migrate_pg
    import pg_sync

    wh.cursor().execute(_OLD_PG_SETUP_SHAPE)
    wh.commit()

    assert pg_sync._prepare_warehouse(wh) == "public"
    assert {"conversation_id", "content_hash", "variant", "user_id"} <= _cols(wh, "public", "memory_items")
    assert not _tables(wh, "m3_warehouse"), "an older warehouse is migrated in place, not moved"
    cur = wh.cursor()
    cur.execute("SELECT content FROM public.memory_items WHERE id = 'm1'")
    assert cur.fetchone()[0] == "kept"
    wh.rollback()
    assert migrate_pg.get_applied_versions(wh, "public") == [1, 2]


def test_unqualified_names_resolve_after_prepare_even_after_a_rollback(wh):
    """The sync's table names are unqualified. The search_path SET was undone by
    the stranded-table check's rollback, so every warehouse table read as missing
    ("relation memory_items does not exist") on a canonical warehouse."""
    import pg_sync

    assert pg_sync._prepare_warehouse(wh) == "m3_warehouse"
    wh.rollback()
    cur = wh.cursor()
    cur.execute("SELECT count(*) FROM memory_items")
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT current_schema()")
    assert cur.fetchone()[0] == "m3_warehouse"


@pytest.fixture
def other_role(wh):
    """A login role that does NOT own the warehouse tables but may use and create
    in the schema, as on a warehouse an admin set up; dropped afterwards."""

    name = f"m3_wh_role_{uuid.uuid4().hex[:8]}"
    cur = wh.cursor()
    cur.execute(f"CREATE ROLE {name} LOGIN PASSWORD 'test-only'")
    cur.execute(f"GRANT CONNECT, CREATE, TEMP ON DATABASE {wh.info.dbname} TO {name}")
    wh.commit()
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(_with_db(pg_dsn(), wh.info.dbname))
    host = parts.hostname or "127.0.0.1"  # a socket DSN has no host; the role needs TCP + password
    netloc = f"{name}:test-only@{host}" + (f":{parts.port}" if parts.port else "")
    dsn = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    try:
        yield name, dsn
    finally:
        wh.rollback()
        cur = wh.cursor()
        cur.execute(f"DROP OWNED BY {name}")
        cur.execute(f"DROP ROLE {name}")
        wh.commit()


def _hand_warehouse_to(wh, role):
    """The tables stay owned by the creator; ``role`` gets the grants sync needs."""
    cur = wh.cursor()
    cur.execute(f"GRANT USAGE, CREATE ON SCHEMA m3_warehouse TO {role}")
    cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA m3_warehouse TO {role}")
    cur.execute("DELETE FROM m3_warehouse.schema_versions")  # as on a warehouse no m3 migrated yet
    wh.commit()


def test_a_role_that_does_not_own_the_tables_upgrades_an_up_to_date_warehouse(wh, other_role):
    """Found on a real warehouse: tables created by postgres, m3 connecting as
    another role. The baseline's ADD COLUMN IF NOT EXISTS failed "must be owner of
    table memory_items" even though nothing was missing."""
    import migrate_pg
    import pg_sync
    import psycopg2

    pg_sync._prepare_warehouse(wh)  # creates every table, owned by the admin role
    role, dsn = other_role
    _hand_warehouse_to(wh, role)
    conn = psycopg2.connect(dsn)
    try:
        assert migrate_pg.run_warehouse_migrations(conn, "m3_warehouse") == [1, 2]
        assert migrate_pg.get_applied_versions(conn, "m3_warehouse") == [1, 2]
    finally:
        conn.close()


def test_a_needed_change_on_a_table_this_role_does_not_own_names_the_fix(wh, other_role):
    import migrate_pg
    import pg_sync
    import psycopg2

    pg_sync._prepare_warehouse(wh)
    cur = wh.cursor()
    cur.execute("ALTER TABLE m3_warehouse.memory_items DROP COLUMN variant")  # an older shape
    wh.commit()
    role, dsn = other_role
    _hand_warehouse_to(wh, role)
    conn = psycopg2.connect(dsn)
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as e:
            migrate_pg.run_warehouse_migrations(conn, "m3_warehouse")
    finally:
        conn.close()
    msg = str(e.value)
    assert "memory_items" in msg and "variant" in msg and f"OWNER TO {role}" in msg, msg


def test_tables_owned_by_another_role_are_reported(wh, other_role):
    """What pg_setup --check reports, before a migration fails on it."""
    import migrate_pg
    import pg_sync
    import psycopg2

    pg_sync._prepare_warehouse(wh)
    assert migrate_pg.tables_owned_elsewhere(wh, "m3_warehouse") == {}, "the creator owns everything"
    role, dsn = other_role
    _hand_warehouse_to(wh, role)
    conn = psycopg2.connect(dsn)
    try:
        others = migrate_pg.tables_owned_elsewhere(conn, "m3_warehouse")
    finally:
        conn.close()
    assert "memory_items" in others and set(others.values()) == {wh.info.user}, others


def test_rows_stranded_in_public_are_reported(wh, caplog):
    import pg_sync

    pg_sync._prepare_warehouse(wh)  # canonical layout
    cur = wh.cursor()
    cur.execute("CREATE TABLE public.tasks (id TEXT PRIMARY KEY)")
    cur.execute("INSERT INTO public.tasks VALUES ('t1')")
    wh.commit()
    with caplog.at_level(logging.WARNING, logger="pg_sync"):
        pg_sync._prepare_warehouse(wh)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "tasks" in msgs and "migrate_warehouse_to_schema.py" in msgs, msgs


def test_a_client_behind_the_warehouse_logs_once_and_continues(wh, caplog, monkeypatch):
    import migrate_pg
    import pg_sync

    pg_sync._prepare_warehouse(wh)
    cur = wh.cursor()
    cur.execute("INSERT INTO m3_warehouse.schema_versions (version, filename) VALUES (999, 'pg_999_future.up.sql')")
    wh.commit()
    monkeypatch.setattr(migrate_pg, "_warned_behind", set())
    with caplog.at_level(logging.INFO, logger="migrate_pg"):
        assert migrate_pg.run_warehouse_migrations(wh, "m3_warehouse") == []
        migrate_pg.run_warehouse_migrations(wh, "m3_warehouse")
    behind = [r for r in caplog.records if "this m3 knows up to" in r.getMessage()]
    assert len(behind) == 1


def test_pg_sync_issues_no_warehouse_ddl():
    """The schema comes from the chain; pg_sync only reads and writes rows."""
    src = (ROOT / "bin" / "pg_sync.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"\b(CREATE\s+(TABLE|INDEX)|ALTER\s+TABLE)\b", code), "pg_sync must not issue DDL"


# ── Pulls by arrival time (pg_002) ────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    """A local SQLite store with the full main schema, foreign keys enforced as
    the sync's connection enforces them."""
    import sqlite3

    from conftest import create_full_main_schema

    db = tmp_path / "local.db"
    create_full_main_schema(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def _wh_item(wh, mid, kind="note", created="2026-01-01T00:00:00+00:00"):
    cur = wh.cursor()
    cur.execute(
        "INSERT INTO memory_items (id, type, content, created_at, updated_at) VALUES (%s, %s, 'x', %s, NULL)",
        (mid, kind, created),
    )
    wh.commit()


def _local_ids(store):
    return {r[0] for r in store.execute("SELECT id FROM memory_items")}


def test_the_warehouse_stamps_when_a_row_arrived(wh):
    import pg_sync

    pg_sync._prepare_warehouse(wh)
    _wh_item(wh, "m1")
    cur = wh.cursor()
    cur.execute("SELECT wh_arrived_at FROM memory_items WHERE id = 'm1'")
    first = cur.fetchone()[0]
    assert first is not None
    cur.execute("UPDATE memory_items SET content = 'y' WHERE id = 'm1'")
    cur.execute("SELECT wh_arrived_at FROM memory_items WHERE id = 'm1'")
    assert cur.fetchone()[0] > first, "an update is a new arrival"
    wh.rollback()


def test_a_row_pushed_after_another_machines_pull_still_reaches_it(wh, store):
    """Found on the real warehouse: a memory written on one machine before another
    machine's pull, and pushed after it, was older than that machine's next
    watermark and was never pulled there (2,011 rows missing on a Mac)."""
    import pg_sync

    pg_sync._prepare_warehouse(wh)
    _wh_item(wh, "m1", created="2026-01-01T00:00:00+00:00")
    pg_sync.sync_memory_items(store.cursor(), wh.cursor(), store, "main")
    assert "m1" in _local_ids(store)
    # Written on the other machine an hour BEFORE that pull, pushed only now.
    _wh_item(wh, "late", created="2026-01-01T00:00:00+00:00")
    pg_sync.sync_memory_items(store.cursor(), wh.cursor(), store, "main")
    assert "late" in _local_ids(store)


def test_each_store_takes_only_its_own_kind(wh, store):
    """Pulling everything into both stores put every chat turn in the memory store
    and every memory in the chat store, on every split install."""
    import pg_sync

    pg_sync._prepare_warehouse(wh)
    _wh_item(wh, "mem", kind="note")
    _wh_item(wh, "turn", kind="chat_log")
    pg_sync.sync_memory_items(store.cursor(), wh.cursor(), store, "main", "memory")
    assert _local_ids(store) == {"mem"}


def test_an_edge_waits_for_a_memory_this_store_does_not_hold(wh, store):
    """An edge to a memory not here yet failed the whole batch on the foreign key,
    every run (49 edges on the Mac); it now waits, and lands once the memory does."""
    import pg_sync

    pg_sync._prepare_warehouse(wh)
    _wh_item(wh, "a")
    _wh_item(wh, "b")
    cur = wh.cursor()
    cur.execute(
        "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type) VALUES ('e1', 'a', 'b', 'related')"
    )
    wh.commit()
    store.execute("INSERT INTO memory_items (id, type, content) VALUES ('a', 'note', 'x')")
    store.commit()
    pg_sync._FAILED_DIRECTIONS.clear()

    pg_sync.sync_memory_relationships(store.cursor(), wh.cursor(), store, "main")
    assert pg_sync._FAILED_DIRECTIONS == [], "a waiting edge is not a failure"
    assert store.execute("SELECT count(*) FROM memory_relationships").fetchone()[0] == 0

    store.execute("INSERT INTO memory_items (id, type, content) VALUES ('b', 'note', 'x')")
    store.commit()
    pg_sync.sync_memory_relationships(store.cursor(), wh.cursor(), store, "main")
    assert store.execute("SELECT id FROM memory_relationships").fetchall()[0][0] == "e1"
