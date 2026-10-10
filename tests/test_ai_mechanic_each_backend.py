"""ai_mechanic's emergency repair runs on EVERY backend, from the owner DDL.

It refused on PostgreSQL (require_sqlite_backend) and recreated the two tables
from its own SQLite DDL, a second copy of the schema. It now drops them and
replays their definition from the active backend's migrations.
"""

from __future__ import annotations

import pytest


def _cols(each_backend, table: str) -> set[str]:
    from memory.backends import dialect

    sql, params = dialect().columns_of(table)
    with each_backend.connection() as conn:
        return {r[0] for r in conn.execute(sql, params).fetchall()}


def _flags(each_backend) -> list[str]:
    if not each_backend.stores_databases_as_files:
        return []
    from m3_sdk import resolve_db_path

    return ["--database", resolve_db_path(None)]


def test_repair_recreates_from_the_owner_schema(each_backend, capsys):
    import ai_mechanic as am

    with each_backend.connection() as conn:
        conn.execute("INSERT INTO project_decisions (project, decision) VALUES ('probe', 'x')")
        conn.commit()
    before = {t: _cols(each_backend, t) for t in am.REPAIRED}

    assert am.main([*_flags(each_backend), "--force"]) == 0
    out = capsys.readouterr().out
    where = f"on {each_backend.name}. inspect: bin/ai_mechanic.py"
    assert "Database Schema Aligned" in out, f"observed: {out!r} {where}"
    assert {t: _cols(each_backend, t) for t in am.REPAIRED} == before, f"observed: columns changed {where}"
    with each_backend.connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM project_decisions").fetchone()[0]
        focus = conn.execute("SELECT id, summary FROM system_focus").fetchall()
    assert n == 0 and [tuple(r) for r in focus] == [(1, "System Repaired & Calibrated")], (
        f"observed: {n} decisions, focus {focus!r} {where}"
    )


def test_guard_rails(each_backend):
    import ai_mechanic as am

    with pytest.raises(SystemExit):
        am.main(_flags(each_backend))  # no --force
    with pytest.raises(SystemExit):
        if each_backend.stores_databases_as_files:
            am.main(["--force"])  # SQLite: no explicit --database
        else:
            am.main(["--database", "agent_memory.db", "--force"])  # PostgreSQL: a path is refused
