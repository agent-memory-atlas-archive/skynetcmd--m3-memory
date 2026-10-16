---
tool: bin/migrate_pg.py
sha1: 3a98d7c96758
mtime_utc: 2026-10-16T17:36:26.428341+00:00
generated_utc: 2026-10-16T19:03:59.649009+00:00
private: false
---

# bin/migrate_pg.py

## Purpose

PostgreSQL PRIMARY-store migration runner — the PG analogue of migrate_memory.py.

The SQLite primary store evolves via numbered ``memory/migrations/NNN_*.up.sql``
files applied by ``migrate_memory.py``. Those files use SQLite-only DDL
(AUTOINCREMENT, FTS5, rowid) and cannot run on PostgreSQL, so PG got a
hand-translated cumulative baseline (``postgres/pg_primary_v1.sql``, stamped
version 39). This runner continues the sequence from 40 with **PG-native**
incremental files:

    memory/migrations/postgres/pg_NNN_<name>.up.sql     (required)
    memory/migrations/postgres/pg_NNN_<name>.down.sql   (optional)

Contract mirrors migrate_memory.py deliberately (discover → order by NNN → track
applied in ``schema_versions`` → apply/stamp; ``down`` reverts + un-stamps), so
the two runners behave the same way. Differences that are intrinsic to the engine:

  * No SAVEPOINT/executescript dance. psycopg2 runs a multi-statement string in one
    real transaction; a file either commits whole or rolls back whole. Migration
    files therefore MUST NOT contain their own COMMIT/ROLLBACK/BEGIN (same rule as
    the SQLite runner; enforced by ``_validate_migration_sql``).
  * DSN resolution goes through the PRIMARY-store resolver and BOTH of the
    backend's guards (``M3_PG_FORBIDDEN_HOSTS`` and same-database-as-the-stored-
    warehouse-DSN), so a DSN naming the warehouse is refused, --dsn included.

Commands: ``up`` (apply pending), ``status``, ``down --to N`` (revert to N),
``plan`` (print pending SQL). ``ensure_schema()`` on the backend still applies the
v39 baseline; this runner takes it from 40 onward. They compose: run the backend's
``ensure_schema`` once to get the baseline, then ``migrate_pg up`` for increments —
or call :func:`run_pending_pg_migrations` programmatically (what the backend does).

---

## Entry points

- `def main()` (line 478)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dsn` | Explicit PRIMARY-store DSN. Default: the stored M3_PRIMARY_PG_URL secret (`m3 secret set M3_PRIMARY_PG_URL`); never read from the environment, never the warehouse DSN. | None |  | str |  |
| `--to` | Stop at this version. | None |  | int |  |
| `--yes` | Skip confirmation. | `False` |  | store_true |  |
| `--dry-run` | Show plan, apply nothing. | `False` |  | store_true |  |
| `--to` | Version to roll back TO (required). | None |  | int |  |
| `--yes` | Skip confirmation. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (PgRole, resolve_pg_dsn)`
- `migrate_memory (_confirm)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.pgconn (pg_connect)`
- `memory.backends.dialect (dialect_for)`
- `memory.backends.postgres_backend (_reject_forbidden_host, _reject_same_as_warehouse)`
- `psycopg2`

---

## File dependencies (repo paths referenced)

- `_session_helpers.sql`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
