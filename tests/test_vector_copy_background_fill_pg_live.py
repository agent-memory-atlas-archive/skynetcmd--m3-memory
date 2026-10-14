"""pg_066 fills the vector copy of existing rows in the background, right after it
is applied, so an upgraded store reaches in-database search without waiting for
the cognitive loop; a fresh store (nothing to fill) starts no thread.

Each test uses its own throwaway database: ensure_schema applies the whole chain.
Skips where pgvector is unavailable.
"""

from __future__ import annotations

import re
import struct
import sys
import threading
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "bin"))

from conftest import skip_reason  # noqa: E402

pytestmark = pytest.mark.requires_pg

_DOWN = _ROOT / "memory" / "migrations" / "postgres" / "pg_066_pgvector_column.down.sql"
_THREAD = "m3-vector-copy-fill"


def _with_db(dsn: str, dbname: str) -> str:
    return re.sub(r"/[^/?]*(\?|$)", f"/{dbname}\\1", dsn, count=1)


@pytest.fixture
def fresh_dsn(pg_url):
    import psycopg2

    name = f"m3_vcf_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(_with_db(pg_url, "postgres"))
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_db(pg_url, name)
    finally:
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _fill_threads():
    return [t for t in threading.enumerate() if t.name == _THREAD]


def _backend(dsn):
    from memory.backends.postgres_backend import PostgresBackend

    return PostgresBackend(dsn=dsn)


def _require_copy(backend):
    with backend.connection() as conn:
        status = backend.vector_copy_status(conn)
    if "memory_embeddings" not in status.missing:
        backend.close()
        pytest.skip(skip_reason("dep", "pgvector is not available on this PostgreSQL server"))


def test_a_fresh_store_starts_no_fill_thread(fresh_dsn):
    before = len(_fill_threads())
    b = _backend(fresh_dsn)
    try:
        b.ensure_schema()
        _require_copy(b)
        assert len(_fill_threads()) == before
    finally:
        b.close()


def test_an_upgraded_store_is_filled_in_the_background(fresh_dsn):
    b = _backend(fresh_dsn)
    b.ensure_schema()
    _require_copy(b)
    # Turn it back into a store from before pg_066, with embeddings in it.
    blobs = {f"e{i}": struct.pack("3f", i + 0.5, -1.0, 2.0) for i in range(5)}
    with b.connection() as conn:
        cur = conn.cursor()
        cur.execute(_DOWN.read_text(encoding="utf-8"))
        cur.execute("DELETE FROM schema_versions WHERE version = 66")
        for i, (eid, blob) in enumerate(blobs.items()):
            cur.execute("INSERT INTO memory_items (id, type, content) VALUES (%s, 'note', 'x')", (f"m{i}",))
            cur.execute(
                "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim) VALUES (%s, %s, %s, 'mod', 3)",
                (eid, f"m{i}", blob),
            )
        conn.commit()
    b.close()

    upgraded = _backend(fresh_dsn)  # a new process's first use applies pg_066 again
    try:
        upgraded.ensure_schema()
        threads = _fill_threads()
        assert threads, "observed: pg_066 was applied to a store with rows but no fill thread started"
        for t in threads:
            t.join(timeout=60)
        with upgraded.connection() as conn:
            assert upgraded.vector_copy_status(conn).missing["memory_embeddings"] == 0
            cur = conn.cursor()
            cur.execute("SELECT id, embedding_vec::text FROM memory_embeddings ORDER BY id")
            got = dict(cur.fetchall())
        assert got == {eid: "[" + ",".join(f"{v:g}" for v in struct.unpack("3f", b)) + "]" for eid, b in blobs.items()}
    finally:
        upgraded.close()
