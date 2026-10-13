"""PostgreSQL: the dispatch chain runs in its own schema with its own versions.

``migrate_pg.get_applied_versions`` used to read ``public.schema_versions`` only,
so the dispatch schema would have seen the MAIN store's versions and skipped its
own pg_001. ``run_pending_pg_migrations(..., schema=)`` keeps the chains apart and
restores the session search_path.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from conftest import pg_dsn, use_backend

pytestmark = pytest.mark.requires_pg

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pg_conn():
    import psycopg2

    use_backend("postgres", pg_dsn())
    conn = psycopg2.connect(pg_dsn())
    schema = f"m3_dispatch_t{uuid.uuid4().hex[:8]}"
    try:
        yield conn, schema
    finally:
        conn.rollback()
        cur = conn.cursor()
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


def test_the_dispatch_schema_keeps_its_own_versions(pg_conn):
    import migrate_pg

    conn, schema = pg_conn
    cur = conn.cursor()
    cur.execute("SHOW search_path")
    before = cur.fetchone()[0]

    chain = str(ROOT / "memory" / "dispatch_migrations" / "postgres")
    assert migrate_pg.run_pending_pg_migrations(conn, chain, schema=schema) == [1]
    assert migrate_pg.get_applied_versions(conn, schema) == [1]
    assert migrate_pg.run_pending_pg_migrations(conn, chain, schema=schema) == []

    cur.execute("SELECT to_regclass(%s)", (f'"{schema}".notification_dispatch',))
    assert cur.fetchone()[0] is not None
    cur.execute("SHOW search_path")
    assert cur.fetchone()[0] == before, "the session search_path was not restored"
