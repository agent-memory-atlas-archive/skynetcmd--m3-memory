---
tool: bin/pg_setup.py
sha1: 801fca59e1f5
mtime_utc: 2026-10-14T15:50:54.674497+00:00
generated_utc: 2026-10-14T15:51:13.514098+00:00
private: false
---

# bin/pg_setup.py

## Purpose

Bring the PostgreSQL data warehouse to its current schema, or report where it is.

The warehouse schema comes from its migration chain (memory/warehouse_migrations/),
which pg_sync applies automatically before every run.
This is the manual entry point for the same thing:

    python bin/pg_setup.py           # apply pending warehouse migrations
    python bin/pg_setup.py --check   # report the layout and versions; change nothing

The warehouse DSN is the M3_CDW_PG_URL secret (m3 secret set M3_CDW_PG_URL).

---

## Entry points

- `def main()` (line 37)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--check` | report the layout and versions; change nothing | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `migrate_pg`
- `pg_sync`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.secret_store (PgRole, resolve_pg_dsn)`
- `psycopg2`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
