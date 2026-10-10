"""The v1 -> v2 entity-vocabulary rename runs on EVERY backend.

It refused on PostgreSQL (require_sqlite_backend). A PostgreSQL store migrated
from a pre-2026-05-03 SQLite one carries the v1 names as copied, so the repair
is needed there too; SQL now comes from the dialect.
"""

from __future__ import annotations

import sys
import uuid

import pytest


@pytest.fixture
def v1_rows(each_backend):
    """Two v1-typed entities joined by a v1 predicate; removed afterwards
    (the edge goes with them, ON DELETE CASCADE)."""
    from memory.backends import dialect

    p = dialect().param()
    tag = uuid.uuid4().hex[:8]
    a, b = f"ent-{tag}-a", f"ent-{tag}-b"
    with each_backend.connection() as conn:
        conn.execute(
            f"INSERT INTO entities (id, canonical_name, entity_type) VALUES ({p}, {p}, 'concept')", (a, f"idea {tag}")
        )
        conn.execute(
            f"INSERT INTO entities (id, canonical_name, entity_type) VALUES ({p}, {p}, 'object')", (b, f"thing {tag}")
        )
        conn.execute(
            f"INSERT INTO entity_relationships (from_entity, to_entity, predicate) VALUES ({p}, {p}, 'relates_to')",
            (a, b),
        )
        conn.commit()
    try:
        yield a, b
    finally:
        with each_backend.connection() as conn:
            conn.execute(f"DELETE FROM entity_relationships WHERE from_entity = {p}", (a,))
            conn.execute(f"DELETE FROM entities WHERE id IN ({p}, {p})", (a, b))
            conn.commit()


def _run(monkeypatch, *argv: str) -> int:
    import migrate_entity_vocab as mv

    monkeypatch.setattr(sys, "argv", ["migrate_entity_vocab", *argv])
    return mv.main()


def test_renames_and_is_idempotent(each_backend, v1_rows, monkeypatch, capsys):
    from memory.backends import dialect

    a, b = v1_rows
    p = dialect().param()
    where = f"on {each_backend.name}. inspect: bin/migrate_entity_vocab.py"

    assert _run(monkeypatch, "--dry-run") == 0
    with each_backend.connection() as conn:
        assert conn.execute(f"SELECT entity_type FROM entities WHERE id = {p}", (a,)).fetchone()[0] == "concept", (
            f"observed: --dry-run wrote {where}"
        )

    assert _run(monkeypatch) == 0
    with each_backend.connection() as conn:
        types = dict(conn.execute(f"SELECT id, entity_type FROM entities WHERE id IN ({p}, {p})", (a, b)).fetchall())
        pred = conn.execute(f"SELECT predicate FROM entity_relationships WHERE from_entity = {p}", (a,)).fetchone()[0]
        stamped = conn.execute(f"SELECT updated_at FROM entities WHERE id = {p}", (a,)).fetchone()[0]
    assert types == {a: "legacy_concept", b: "legacy_object"}, f"observed: {types!r} {where}"
    assert pred == "mentions" and stamped is not None, f"observed: {pred!r}, updated_at={stamped!r} {where}"

    capsys.readouterr()
    assert _run(monkeypatch) == 0
    assert "Nothing to do" in capsys.readouterr().out, f"observed: a second pass found work {where}"


def test_database_on_a_server_backend_is_refused(each_backend, monkeypatch, capsys):
    if each_backend.stores_databases_as_files:
        assert _run(monkeypatch, "--database", "no-such-dir/none.db") == 2
        assert "observed: no SQLite DB at" in capsys.readouterr().err
    else:
        assert _run(monkeypatch, "--database", "agent_memory.db") == 2
        assert "observed: --database names a SQLite file" in capsys.readouterr().err
