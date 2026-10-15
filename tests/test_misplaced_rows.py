"""Copies an older sync put in the wrong store of a split install (doctor/misplaced_rows):
removed only when the home store holds the same row, its links and an embedding."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

_SCHEMA = """
CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, content TEXT, content_hash TEXT,
                           is_deleted INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE memory_embeddings (id TEXT PRIMARY KEY, memory_id TEXT, embedding BLOB);
CREATE TABLE memory_relationships (id TEXT PRIMARY KEY, from_id TEXT, to_id TEXT, relationship_type TEXT);
CREATE TABLE memory_item_entities (memory_id TEXT, entity_id TEXT);
"""


def _store(path):
    c = sqlite3.connect(path)
    c.executescript(_SCHEMA)
    return c


def _item(c, mid, kind, content="x", deleted=0, updated="2026-10-01", hash_="h"):
    c.execute(
        "INSERT INTO memory_items VALUES (?,?,?,?,?,?)",
        (mid, "chat_log" if kind == "chat" else "note", content, hash_ + content, deleted, updated),
    )


@pytest.fixture
def split(tmp_path, monkeypatch, as_sqlite):
    import pg_sync
    from doctor import misplaced_rows as mr

    mem, chat = str(tmp_path / "memory.db"), str(tmp_path / "chat.db")
    m, c = _store(mem), _store(chat)
    from memory.backends.dialect import chatlog_table_for

    core = {r: chatlog_table_for(r, "sqlite") for r in pg_sync._CORE_ROLES}
    targets = [
        pg_sync.SyncTarget("main", mem, dict(core), kind="memory"),
        pg_sync.SyncTarget("chatlog", chat, dict(core), kind="chat"),
    ]
    monkeypatch.setattr(mr, "_split_targets", lambda: targets)
    return mr, m, c


@pytest.fixture
def as_sqlite():
    from conftest import select_backend

    select_backend("sqlite")


def _ids(c, table="memory_items", col="id"):
    return {r[0] for r in c.execute(f"SELECT {col} FROM {table}")}


def test_only_copies_held_the_same_at_home_are_removed(split):
    mr, m, c = split
    # chat turns: at home in the chat store; copies in the memory store
    for mid in ("same", "edited", "newer", "deleted_here", "lonely"):
        _item(
            m,
            mid,
            "chat",
            updated="2026-10-02" if mid == "newer" else "2026-10-01",
            content="old" if mid == "edited" else "x",
            deleted=1 if mid == "deleted_here" else 0,
        )
    for mid in ("same", "edited", "newer", "deleted_here"):
        _item(c, mid, "chat")
    # the home copy was deleted later; the copy is the older, live version
    _item(m, "home_deleted_later", "chat")
    _item(c, "home_deleted_later", "chat", deleted=1, updated="2026-10-05")
    _item(m, "linked", "chat")
    _item(c, "linked", "chat")
    m.execute("INSERT INTO memory_relationships VALUES ('e-missing', 'linked', 'n1', 'related')")
    _item(m, "embedded", "chat")
    _item(c, "embedded", "chat")
    m.execute("INSERT INTO memory_embeddings VALUES ('v1', 'embedded', x'00')")
    m.execute("INSERT INTO memory_item_entities VALUES ('same', 'ent1')")
    m.execute("INSERT INTO memory_embeddings VALUES ('v2', 'same', x'00')")
    c.execute("INSERT INTO memory_embeddings VALUES ('v3', 'same', x'00')")
    # a real memory, at home in the memory store, copied into the chat store with its link
    _item(m, "n1", "memory")
    _item(m, "n2", "memory")
    _item(c, "n1", "memory")
    m.execute("INSERT INTO memory_relationships VALUES ('e1', 'n1', 'n2', 'related')")
    c.execute("INSERT INTO memory_relationships VALUES ('e1', 'n1', 'n2', 'related')")
    m.commit()
    c.commit()

    before = {r.store: r for r in mr.check()}
    assert (before["main"].stray, before["main"].removable) == (8, 3)
    assert before["main"].kept == {
        "content differs from its home copy": 1,
        "newer than its home copy": 1,
        "its only copy": 1,
        "has links its home store lacks": 1,
        "has an embedding its home copy lacks": 1,
    }
    assert (before["chatlog"].stray, before["chatlog"].removable) == (1, 1)
    assert _ids(m) >= {"same"}, "a check alone removes nothing"

    after = {r.store: r for r in mr.check(repair=True)}
    assert after["main"].removed == 3 and after["chatlog"].removed == 1
    gone = {"same", "deleted_here", "home_deleted_later"}
    assert not gone & _ids(m) and gone <= _ids(c), "the home copy is the record of its kind"
    assert {"edited", "newer", "lonely", "linked", "embedded", "n1", "n2"} <= _ids(m)
    assert "n1" not in _ids(c) and "n1" in _ids(m)
    assert _ids(m, "memory_embeddings") == {"v1"}, "the removed copy's embedding goes with it"
    assert _ids(c, "memory_embeddings") == {"v3"}
    assert not _ids(m, "memory_item_entities", "memory_id")
    assert _ids(c, "memory_relationships") == set() and _ids(m, "memory_relationships") == {"e1", "e-missing"}
    assert all(r.stray == r.removed + sum(r.kept.values()) for r in after.values())


def test_a_unified_install_has_nothing_to_check(monkeypatch):
    from doctor import misplaced_rows as mr

    monkeypatch.setattr(mr, "_split_targets", list)
    assert mr.check(repair=True) == []


@pytest.mark.requires_pg
def test_postgres_removes_only_the_copys_family():
    """One database holds both kinds (memory_items and chat_log_items, each with
    its own dependents): the copy goes with its own rows, the home copy keeps all
    of its own."""
    from conftest import pg_dsn, use_backend

    use_backend("postgres", pg_dsn())
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=pg_dsn())
    b._schema_ready = False
    b.ensure_schema()
    import migrate_pg

    with b.connection() as conn:
        migrate_pg.run_pending_pg_migrations(conn)
    turn, ent = str(uuid.uuid4()), str(uuid.uuid4())
    with b.connection() as conn:
        cur = conn.cursor()
        for table in ("chat_log_items", "memory_items"):
            cur.execute(
                f"INSERT INTO {table} (id, type, title, content, content_hash, updated_at) "
                "VALUES (%s, 'chat_log', '', 'hello', 'hh', '2026-10-01T00:00:00+00:00')",
                (turn,),
            )
        for table in ("chat_log_embeddings", "memory_embeddings"):
            cur.execute(
                f"INSERT INTO {table} (id, memory_id, embedding) VALUES (%s, %s, %s)",
                (str(uuid.uuid4()), turn, b"x"),
            )
        cur.execute("INSERT INTO entities (id, canonical_name, entity_type) VALUES (%s, 'x', 'thing')", (ent,))
        cur.execute("INSERT INTO memory_item_entities (memory_id, entity_id) VALUES (%s, %s)", (turn, ent))
        conn.commit()
    from doctor import misplaced_rows as mr

    def count(cur, table, col="memory_id"):
        cur.execute(f"SELECT count(*) FROM {table} WHERE {col} = %s", (turn,))
        return cur.fetchone()[0]

    try:
        reports = {r.store: r for r in mr.check(repair=True)}
        assert reports["main"].removed >= 1
        with b.connection() as conn:
            cur = conn.cursor()
            assert (count(cur, "memory_items", "id"), count(cur, "memory_embeddings")) == (0, 0)
            assert count(cur, "memory_item_entities") == 0
            assert (count(cur, "chat_log_items", "id"), count(cur, "chat_log_embeddings")) == (1, 1)
    finally:
        with b.connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM chat_log_items WHERE id = %s", (turn,))
            cur.execute("DELETE FROM memory_items WHERE id = %s", (turn,))
            cur.execute("DELETE FROM entities WHERE id = %s", (ent,))
            conn.commit()
        b.close()


def test_the_doctor_points_at_the_fix_only_when_it_would_remove_something(monkeypatch, capsys):
    """'0 removable: m3 doctor --fix' sent people to a command that did nothing."""
    from doctor import misplaced_rows as mr

    kept = mr.StoreReport("chatlog", "chat", stray=3, removable=0, kept={"its only copy": 3})
    monkeypatch.setattr(mr, "check", lambda repair=False: [kept])
    mr.run(brief=True)
    mr.run(brief=False)
    mr.repair_and_report()
    out = capsys.readouterr().out
    assert "--fix" not in out and "kept in the wrong store" in out and "[fixed]" not in out
    kept.removable = 1
    mr.run(brief=True)
    assert "1 removable: m3 doctor --fix" in capsys.readouterr().out
