"""Schema parity: the PostgreSQL primary schema must carry the same core tables
and columns as the SQLite schema.

The two schemas are maintained by hand in lock-step — SQLite via
memory/migrations/*.sql, PostgreSQL via memory/migrations/postgres/pg_primary_v1
+ pg_NNN. Nothing ENFORCED that they stay aligned, so a column added to a SQLite
migration but not mirrored into a pg_ migration would ship undetected and only
surface as a runtime error on a PG deployment. This test compares the column
NAME sets of the shared core tables and fails on any divergence.

We compare names, not types: TEXT vs TIMESTAMPTZ / INTEGER vs BIGINT are expected
representation differences, not drift. A missing or extra COLUMN is the drift we
care about. Chatlog `chat_log_*` tables are PG-only (SQLite keeps chatlog in a
separate file with core names), so they're excluded from the comparison set.

Skips cleanly without a reachable cluster. DSN from M3_PRIMARY_PG_URL/M3_PG_URL
(never PG_URL — that's the warehouse var).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
_MIGRATIONS = Path(__file__).resolve().parents[1] / "memory" / "migrations"
sys.path.insert(0, str(_BIN))


# Gated by the requires_pg marker: conftest's collection hook auto-skips this
# module when no Postgres is reachable. pg_dsn() centralizes the
# M3_PRIMARY_PG_URL > M3_PG_URL precedence (replaces the former per-file
# _dsn()/_reachable()/skipif triplet).
from conftest import pg_dsn, use_backend

pytestmark = pytest.mark.requires_pg


def _dsn() -> str:
    """Resolve the DSN LAZILY, at fixture time — never at module import.

    A module-level ``_DSN = pg_dsn()`` is evaluated during pytest COLLECTION,
    while conftest's autouse sandbox fixture clears M3_PG_URL/PG_URL per test
    (deliberately — an ambient dev DSN must not leak into sqlite-default tests).
    Whether this module saw the var then depends on collection/run ORDERING, so
    the file passes alone and fails in a full lane on `assert _DSN is not None`.
    """
    d = pg_dsn()
    assert d is not None, (
        "no Postgres DSN — set M3_PRIMARY_PG_URL or M3_PG_URL. (The requires_pg "
        "marker should have skipped this module.)"
    )
    return d


# Core tables that both backends are expected to carry with the same columns.
# (Excludes: SQLite-only FTS shadow tables memory_items_fts*, the schema_versions
# bookkeeping table, and PG-only chat_log_* clones.)
_SHARED_CORE_TABLES = {
    "memory_items",
    "memory_embeddings",
    "memory_relationships",
    "memory_history",
    "entities",
    "memory_item_entities",
    "entity_relationships",
    "entity_extraction_queue",
    "observation_queue",
    "gdpr_requests",
    "agent_retention_policies",
    "memory_archive",
    "entity_coalesce_candidates",
    "entity_coalesce_embeddings",
    # Added after pg_051: the encrypted secrets vault existed on SQLite and in the
    # warehouse schema but NOT on the PG primary, and this allowlist never named
    # it -- so the parity gate could not see it. See
    # test_every_sqlite_core_table_exists_on_pg below, which starts from the
    # SQLite schema instead of this list precisely so the next such table is
    # caught without anyone remembering to add it here.
    "synchronized_secrets",
    # Sync bookkeeping, declared by 005/044 (SQLite) and pg_053 (PostgreSQL).
    #
    # ⚠ These are PER-MACHINE state and must NEVER be replicated to the
    # warehouse — a shared watermark makes two machines resume from each other's
    # position, and a shared lock makes one block or steal the other's. That is a
    # constraint on pg_sync's TABLE LIST, not on the schema: both backends must
    # still HAVE the tables, which is what this list checks.
    #
    # (An earlier version of this file excluded sync_watermarks as "SQLite-side
    # by design". That was wrong: sync_all.py created it on the PG primary too,
    # just ad-hoc rather than by migration. Per-machine is not the same as
    # per-backend.)
    "sync_watermarks",
    "sync_locks",
}


@contextmanager
def _fresh_sqlite():
    """A SQLite store built through the full migration chain (three tests each
    carried a copy of this block)."""
    import migrate_memory as m

    d = tempfile.mkdtemp()
    conn = sqlite3.connect(os.path.join(d, "parity.db"))
    try:
        m.init_migrations_table(conn)
        migs = m.discover_migrations(str(_MIGRATIONS))
        for v in sorted(migs):
            m.apply_migration(conn, v, migs[v]["name"], migs[v]["up"])
        yield conn
    finally:
        conn.close()


def _columns(conn, dialect, tables) -> dict:
    """{table: set(column_names)} through the seam (Dialect.columns_of)."""
    out: dict = {}
    for t in tables:
        sql, params = dialect.columns_of(t)
        out[t] = {r[0] for r in conn.execute(sql, params).fetchall()}
    return out


def _sqlite_schema() -> tuple[set[str], dict]:
    """(every real table, {table: columns}) of a freshly migrated SQLite store.

    Enumerates the source of truth: a curated list can only re-check tables
    somebody already listed.
    """
    from memory.backends.dialect import dialect_for
    from memory.backends.sqlite_backend import SqliteBackend

    with _fresh_sqlite() as conn:
        tables = SqliteBackend().list_tables(conn)
        return tables, _columns(conn, dialect_for("sqlite"), tables)


def _sqlite_all_tables() -> set[str]:
    return _sqlite_schema()[0]


def _pg_all_tables(backend) -> set[str]:
    """Every table in the live PG store's active schema, via the seam."""
    with backend.connection() as c:
        return backend.list_tables(c)


def _pg_columns(backend, tables=None) -> dict:
    """{table: set(column_names)} on the live PG store, through the seam."""
    with backend.connection() as c:
        return _columns(c, backend.dialect(), sorted(tables if tables is not None else _SHARED_CORE_TABLES))


@pytest.fixture()
def pg(monkeypatch):
    use_backend("postgres", _dsn())
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_dsn())
    # Own the schema deterministically on the shared cluster: drop every public
    # table so ensure_schema + the migration runner rebuild from scratch.
    with b.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        for (t,) in cur.fetchall():
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    b._schema_ready = False
    b.ensure_schema()
    import migrate_pg

    with b.connection() as c:
        migrate_pg.run_pending_pg_migrations(c)
    yield b
    b.close()


def test_core_tables_exist_on_both(pg):
    """Every shared core table must exist on PG (SQLite builds them by construction)."""
    pg_cols = _pg_columns(pg)
    missing = sorted(t for t in _SHARED_CORE_TABLES if not pg_cols.get(t))
    assert not missing, f"core tables missing from the PostgreSQL schema: {missing}"


# Columns that legitimately exist on ONE backend only (not drift):
#   - search_vector: PG tsvector FTS column (SQLite uses an FTS5 shadow table).
#   - stage1_kg_done / weight: PG-native accelerator columns with no SQLite
#     analogue (used by PG-only indexes / future KG staging).
# Keyed by table -> {backend: {allowed column names}}.
_ALLOWED_BACKEND_ONLY = {
    "memory_items": {"only_pg": {"search_vector", "stage1_kg_done"}},
    "memory_relationships": {"only_pg": {"weight"}},
    # pg_041 added `status` to both extraction queues; SQLite's 038 added it to
    # entity_extraction_queue only. No reader of fact_enrichment_queue.status on
    # either backend (grep, 2026-10-10), so the extra PG column is inert.
    "fact_enrichment_queue": {"only_pg": {"status"}},
}


# SQLite tables that are deliberately absent from the PG primary, with the reason.
# Anything NOT listed here and NOT on PG is drift — see the completeness test.
_SQLITE_ONLY_TABLES = {
    # FTS5 virtual table + its shadow tables; PG uses a tsvector column on
    # memory_items instead. No PG analogue by design.
    "memory_items_fts",
    "memory_items_fts_data",
    "memory_items_fts_idx",
    "memory_items_fts_content",
    "memory_items_fts_docsize",
    "memory_items_fts_config",
    # SQLite migration bookkeeping; PG tracks versions in schema_versions.
    "schema_migrations",
}


def test_every_sqlite_core_table_exists_on_pg(pg):
    """Completeness gate: start from the SQLite SCHEMA, not from a curated list.

    `_SHARED_CORE_TABLES` is hand-maintained, so it can only catch drift in tables
    somebody remembered to add to it. A table that exists on SQLite and was never
    mirrored to PG is INVISIBLE to a check that iterates that list -- which is
    exactly how `synchronized_secrets` (the encrypted secrets vault) shipped
    missing from the PG primary schema until pg_051, silently disabling the vault
    tier of auth_utils.get_api_key on every PostgreSQL deployment.

    Same shape as the `test_every_cli_tool_has_a_page` fix: a gate that iterates
    EXISTING artifacts is a freshness check, not a completeness check. This one
    enumerates the source of truth and requires each entry to be either present
    on PG or explicitly listed as SQLite-only with a reason.
    """
    sqlite_tables = set(_sqlite_all_tables())
    pg_tables = _pg_all_tables(pg)
    missing = sorted(sqlite_tables - pg_tables - _SQLITE_ONLY_TABLES)
    assert not missing, (
        "tables exist on SQLite but not on the PostgreSQL primary schema — add a "
        "pg_NNN migration mirroring them, or list them in _SQLITE_ONLY_TABLES "
        f"with the reason they are backend-specific:\n{missing}"
    )


def test_column_parity_sqlite_vs_pg(pg):
    """Column NAME sets must match per shared core table, except the small set of
    deliberately backend-only columns in _ALLOWED_BACKEND_ONLY. Any OTHER column
    present on one backend but not the other is schema drift — fix the lagging
    migration (this is exactly how the missing gdpr_requests / observation_queue.
    stage on PG were caught before pg_044).

    Iterates EVERY table both backends have, not _SHARED_CORE_TABLES: the
    curated list never named enrichment_groups, so `send_to` (031) shipped
    without a PG migration and `m3_enrich --send-to` failed on PostgreSQL
    (2026-10-10). Same lesson as the table-completeness gate above."""
    sq_tables, sq = _sqlite_schema()
    shared = (sq_tables & _pg_all_tables(pg)) - _SQLITE_ONLY_TABLES
    pgc = _pg_columns(pg, shared)
    drift: dict = {}
    for t in sorted(shared):
        allow = _ALLOWED_BACKEND_ONLY.get(t, {})
        only_sqlite = sq.get(t, set()) - pgc.get(t, set()) - allow.get("only_sqlite", set())
        only_pg = pgc.get(t, set()) - sq.get(t, set()) - allow.get("only_pg", set())
        if only_sqlite or only_pg:
            drift[t] = {"only_sqlite": sorted(only_sqlite), "only_pg": sorted(only_pg)}
    assert not drift, (
        "schema drift between SQLite and PostgreSQL core tables — mirror the "
        f"missing columns into the lagging backend's migrations:\n{drift}"
    )


def test_chroma_tables_absent_on_both(pg):
    """The retired ChromaDB tables must be gone from BOTH schemas (SQLite migration
    040 / the PG baseline never recreates them)."""
    chroma = {"chroma_sync_queue", "chroma_mirror", "chroma_mirror_embeddings", "sync_conflicts", "sync_state"}
    sq_tables = _sqlite_all_tables()
    assert not (chroma & sq_tables), f"chroma tables still in SQLite schema: {chroma & sq_tables}"
    # PG
    with pg.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        pg_tables = {r[0] for r in cur.fetchall()}
    assert not (chroma & pg_tables), f"chroma tables still in PG schema: {chroma & pg_tables}"


def test_chatlog_clones_mirror_core_columns(pg):
    """The chatlog container's chat_log_* clones must carry EVERY column their core
    counterpart has.

    pg_043 clones the core tables into chat_log_* on PG; a column added to a core
    table AFTER that clone (like `status` on entity_extraction_queue via pg_041)
    silently didn't reach the chatlog clone (chat_log_extraction_queue), so a ported
    tool hit a missing column on PG only — chatlog entity extraction couldn't mark a
    turn terminal, so the cognitive loop couldn't idle on PG (fixed by pg_049). This
    guards the whole class: a chat_log_* clone may ADD columns (e.g. search_vector),
    but must never be MISSING one its core source has.
    """
    from memory.backends.dialect import _CHATLOG_TABLES

    def _cols(cur, tbl: str) -> set:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
            (tbl,),
        )
        return {r[0] for r in cur.fetchall()}

    dropped: dict = {}
    with pg.connection() as c:
        cur = c.cursor()
        for core_name, chat_log_name in _CHATLOG_TABLES.values():
            core_cols = _cols(cur, core_name)
            cl_cols = _cols(cur, chat_log_name)
            if not core_cols or not cl_cols:
                continue  # a table absent on this build is covered by other tests
            missing = core_cols - cl_cols
            if missing:
                dropped[chat_log_name] = sorted(missing)
    assert not dropped, f"chatlog clone(s) missing core columns (PG-only parity gap): {dropped}"
