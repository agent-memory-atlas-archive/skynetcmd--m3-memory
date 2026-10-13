"""The warehouse schema comes from its migration chain (user decisions 2026-10-13:
a chain applied automatically by pg_sync; older `public` warehouses migrated in
place; one baseline).

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

    assert migrate_pg.get_applied_versions(wh, "m3_warehouse") == [1]
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
    assert migrate_pg.get_applied_versions(wh, "public") == [1]


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
