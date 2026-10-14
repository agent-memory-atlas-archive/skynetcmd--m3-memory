"""A PostgreSQL store from an older release is migrated on first use, once.

Found by a trial install: a user who chose PostgreSQL with `m3 config set
db.backend postgres` against a store at schema 58 kept it at 58 for good. Writes
worked, `m3 doctor` said HEALTHY, `m3 doctor --fix` said NOTHING_TO_DO, and the
doctor's pgvector repair applied pg_066 on its own, ahead of 59..65. Nothing but
the installer ever called ensure_schema.

Each test builds a schema-58 store in a throwaway database (the base schema plus
the pg migrations up to 58), then checks the behaviour a user reaches.
"""

from __future__ import annotations

import re
import shutil
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PG_MIG = ROOT / "memory" / "migrations" / "postgres"

pytestmark = pytest.mark.requires_pg


def _with_db(dsn: str, dbname: str) -> str:
    return re.sub(r"/[^/?]*(\?|$)", f"/{dbname}\\1", dsn, count=1)


def _known() -> int:
    return max(int(p.name[3:6]) for p in PG_MIG.glob("pg_[0-9][0-9][0-9]_*.up.sql"))


def _version(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(max(version), 0) FROM schema_versions")
    v = int(cur.fetchone()[0])
    conn.rollback()
    return v


@pytest.fixture
def store58(pg_url, tmp_path):
    """A DSN for a database holding a schema-58 m3 store."""
    import migrate_pg
    import psycopg2

    name = f"m3_s58_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(_with_db(pg_url, "postgres"))
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    dsn = _with_db(pg_url, name)
    older = tmp_path / "pg58"
    older.mkdir()
    for p in PG_MIG.glob("pg_[0-9][0-9][0-9]_*.sql"):
        if int(p.name[3:6]) <= 58:
            shutil.copy(p, older / p.name)
    conn = psycopg2.connect(dsn)
    try:
        conn.cursor().execute((PG_MIG / "pg_primary_v1.sql").read_text(encoding="utf-8"))
        conn.commit()
        migrate_pg.run_pending_pg_migrations(conn, str(older))
        assert _version(conn) == 58
    finally:
        conn.close()
    try:
        yield dsn
    finally:
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _backend(dsn):
    from memory.backends.postgres_backend import PostgresBackend

    return PostgresBackend(dsn=dsn)


def test_the_first_connection_migrates_an_older_store(store58):
    b = _backend(store58)
    b.on_selected()  # what the selector does for the store m3 serves
    try:
        with b.connection() as conn:
            assert _version(conn) == _known()
            assert b.schema_status(conn) == (_known(), _known())
        assert b.migrations_applied == list(range(59, _known() + 1))
    finally:
        b.close()


def test_two_processes_starting_together_migrate_once(store58):
    backends = [_backend(store58) for _ in range(3)]
    errors = []

    def go(b):
        try:
            b.ensure_schema()
        except Exception as e:  # noqa: BLE001 — collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=go, args=(b,)) for b in backends]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    try:
        assert not errors, errors
        applied = sorted(v for b in backends for v in b.migrations_applied)
        assert applied == list(range(59, _known() + 1)), "each migration must be applied exactly once"
    finally:
        for b in backends:
            b.close()


def test_the_vector_copy_never_runs_ahead_of_the_chain(store58):
    import psycopg2

    b = _backend(store58)
    try:
        raw = psycopg2.connect(store58)
        try:
            b.ensure_vector_copy(raw)
            raw.commit()
            assert _version(raw) == _known(), "pg_066 must arrive with 59..65, not on its own"
        finally:
            raw.close()
    finally:
        b.close()


def test_doctor_fix_migrates_and_says_so(store58, monkeypatch):
    import importlib

    b = _backend(store58)
    mb = importlib.import_module("memory.backends")
    monkeypatch.setattr(mb, "active_backend", lambda: b)
    monkeypatch.setattr(importlib.import_module("m3_core.paths"), "resolve_backend_name", lambda: "postgres")
    doctor = importlib.import_module("memory.doctor")
    import asyncio

    try:
        res = asyncio.run(doctor.memory_doctor_fix_impl(dry_run=False))
        mig = next(a for a in res["actions"] if a["action"] == "run_migrations")
        assert mig["status"] == "ok", mig
        assert f"current at v{_known()}" in mig["detail"] and "applied [59" in mig["detail"], mig
        assert res["summary"] == "ok"
    finally:
        b.close()


def test_an_instance_built_directly_is_left_alone(store58):
    """A tool or test that builds a backend for its own purpose decides itself."""
    b = _backend(store58)
    try:
        with b.connection() as conn:
            assert _version(conn) == 58
    finally:
        b.close()
