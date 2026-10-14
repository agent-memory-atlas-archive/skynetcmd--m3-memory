"""migrate_pg's per-file transaction guard: a top-level BEGIN/COMMIT/ROLLBACK breaks
the one-transaction-per-file model, but BEGIN/END inside a dollar-quoted body (a DO
block or a function) are PL/pgSQL block keywords and must be allowed (pg_066)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import migrate_pg  # noqa: E402


@pytest.mark.parametrize(
    "sql",
    [
        "DO $$\nBEGIN\n    PERFORM 1;\nEND $$;",
        "CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $fn$\nBEGIN\n  RETURN 1;\nEND $fn$;",
        "-- BEGIN in a comment\nCREATE TABLE t (x int);",
    ],
)
def test_plpgsql_blocks_and_comments_are_allowed(sql):
    migrate_pg._validate_migration_sql("x.up.sql", sql)


@pytest.mark.parametrize("stmt", ["BEGIN;", "COMMIT;", "ROLLBACK;"])
def test_top_level_transaction_control_is_rejected(stmt):
    sql = f"CREATE TABLE t (x int);\n{stmt}\nDO $$ BEGIN PERFORM 1; END $$;"
    with pytest.raises(ValueError, match="top-level"):
        migrate_pg._validate_migration_sql("x.up.sql", sql)
