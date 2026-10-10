"""m3_chatlog_enrich_backfill enqueues chatlog conversations on EVERY backend.

It refused on PostgreSQL (require_sqlite_backend); its defaults named the legacy
repo-relative memory/*.db files, the chatlog read named the SQLite table, and
the commit was a raw COMMIT. On PostgreSQL the chatlog is chat_log_* tables in
the one database, so the path flags are refused there.
"""

from __future__ import annotations

import sys
import uuid

import pytest


@pytest.fixture
def chat(each_backend):
    """Two conversations of chat turns (one older), unique to this run; the
    turns and their queue rows are removed afterwards."""
    from memory.backends import dialect
    from memory.backends.dialect import chatlog_table

    items = chatlog_table("items")
    p = dialect().param()
    tag = uuid.uuid4().hex[:8]
    convs = {f"conv-bf-{tag}-new": "2026-10-09T10:00:00Z", f"conv-bf-{tag}-old": "2026-01-02T10:00:00Z"}
    with each_backend.connection() as conn:
        for i, (cid, ts) in enumerate(convs.items()):
            conn.execute(
                f"INSERT INTO {items} (id, type, content, conversation_id, user_id, created_at) "
                f"VALUES ({p}, 'chat_log', {p}, {p}, {p}, {p})",
                (f"bf-{tag}-{i}", "turn", cid, "u-bf", ts),
            )
        conn.commit()
    try:
        yield sorted(convs)
    finally:
        with each_backend.connection() as conn:
            conn.execute(f"DELETE FROM {items} WHERE id LIKE {p}", (f"bf-{tag}-%",))
            conn.execute(f"DELETE FROM observation_queue WHERE conversation_id LIKE {p}", (f"conv-bf-{tag}-%",))
            conn.commit()


def _run(monkeypatch, *argv: str) -> int:
    import m3_chatlog_enrich_backfill as bf

    monkeypatch.setattr(sys, "argv", ["m3_chatlog_enrich_backfill", *argv])
    return bf.main()


def _flags(each_backend) -> tuple[str, ...]:
    """SQLite: point both stores at the test file (the chatlog is unified here)."""
    if not each_backend.stores_databases_as_files:
        return ()
    from m3_sdk import resolve_db_path

    path = resolve_db_path(None)
    return ("--main-db", path, "--chatlog-db", path)


def test_enqueues_once_and_honours_since(each_backend, chat, monkeypatch, capsys):
    from memory.backends import dialect

    with each_backend.connection():
        pass  # the sandbox materializes the migrated store on first touch
    p = dialect().param()
    new, old = chat[0], chat[1]
    where = f"on {each_backend.name}. inspect: bin/m3_chatlog_enrich_backfill.py"

    assert _run(monkeypatch, *_flags(each_backend), "--since", "2026-10-01", "--yes") == 0
    with each_backend.connection() as conn:
        queued = {
            r[0]
            for r in conn.execute(
                f"SELECT conversation_id FROM observation_queue WHERE conversation_id IN ({p}, {p})", (new, old)
            ).fetchall()
        }
    assert queued == {new}, f"observed: {queued!r} {where}"

    capsys.readouterr()
    assert _run(monkeypatch, *_flags(each_backend), "--since", "2026-10-01", "--yes") == 0
    assert "enqueued 0 new rows" in capsys.readouterr().out, f"observed: a duplicate enqueue {where}"


def test_a_path_on_a_server_backend_is_refused(each_backend, monkeypatch, capsys):
    if each_backend.stores_databases_as_files:
        assert _run(monkeypatch, "--main-db", "no-such-dir/none.db", "--dry-run") == 1
        assert "observed: no SQLite DB at" in capsys.readouterr().err
    else:
        assert _run(monkeypatch, "--chatlog-db", "agent_chatlog.db", "--dry-run") == 1
        assert "observed: --chatlog-db names a SQLite file" in capsys.readouterr().err
