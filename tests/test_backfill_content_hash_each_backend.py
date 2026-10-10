"""backfill_content_hash fills NULL embedding hashes on EVERY backend and store.

It refused on PostgreSQL (require_sqlite_backend) and opened SQLite files by
path. A PostgreSQL store migrated from an old SQLite one carries the NULL
hashes as copied, so the repair is needed there; --target picks the core or
chatlog tables, which on PostgreSQL share one database.
"""
from __future__ import annotations

import uuid

import pytest


@pytest.fixture(params=["core", "chatlog"])
def null_hash_row(each_backend, request):
    """One item + one embedding with a NULL content_hash in the --target store."""
    from memory.backends import dialect
    from memory.backends.dialect import chatlog_table

    target = request.param
    items, embeddings = ((chatlog_table("items"), chatlog_table("embeddings")) if target == "chatlog"
                         else ("memory_items", "memory_embeddings"))
    p = dialect().param()
    tag = uuid.uuid4().hex[:8]
    mid, eid, content = f"bch-{tag}", f"bche-{tag}", f"hash me {tag}"
    with each_backend.connection() as conn:
        conn.execute(f"INSERT INTO {items} (id, type, content) VALUES ({p}, 'chat_log', {p})", (mid, content))
        conn.execute(f"INSERT INTO {embeddings} (id, memory_id, embedding) VALUES ({p}, {p}, {p})",
                     (eid, mid, b"\x00" * 8))
        conn.commit()
    try:
        yield target, embeddings, eid, content
    finally:
        with each_backend.connection() as conn:
            conn.execute(f"DELETE FROM {embeddings} WHERE id = {p}", (eid,))
            conn.execute(f"DELETE FROM {items} WHERE id = {p}", (mid,))
            conn.commit()


def _argv(each_backend, target: str, *extra: str) -> list[str]:
    argv = ["--target", target, "--id-prefix", "bche-", *extra]
    if each_backend.stores_databases_as_files:
        from m3_sdk import resolve_db_path

        argv += ["--db", resolve_db_path(None)]  # the test store is unified on SQLite
    return argv


def test_fills_the_hash_once(each_backend, null_hash_row):
    import backfill_content_hash as bch
    import memory_core as mc
    from memory.backends import dialect

    target, embeddings, eid, content = null_hash_row
    p = dialect().param()
    where = f"on {each_backend.name}/{target}. inspect: bin/backfill_content_hash.py"

    assert bch.main(_argv(each_backend, target, "--dry-run")) == 0
    with each_backend.connection() as conn:
        got = conn.execute(f"SELECT content_hash FROM {embeddings} WHERE id = {p}", (eid,)).fetchone()[0]
    assert got is None, f"observed: --dry-run wrote {where}"

    assert bch.main(_argv(each_backend, target)) == 0
    with each_backend.connection() as conn:
        got = conn.execute(f"SELECT content_hash FROM {embeddings} WHERE id = {p}", (eid,)).fetchone()[0]
    assert got == mc._content_hash(content), f"observed: {got!r} {where}"


def test_db_on_a_server_backend_is_refused(each_backend, capsys):
    import backfill_content_hash as bch

    if each_backend.stores_databases_as_files:
        assert bch.main(["--db", "no-such-dir/none.db"]) == 2
        assert "observed: no SQLite DB at" in capsys.readouterr().err
    else:
        assert bch.main(["--db", "agent_memory.db"]) == 2
        assert "observed: --db names a SQLite file" in capsys.readouterr().err
