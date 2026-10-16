---
tool: bin/pg_sync.py
sha1: ce5274194c0c
mtime_utc: 2026-10-16T17:53:07.282256+00:00
generated_utc: 2026-10-16T19:03:59.691834+00:00
private: false
---

# bin/pg_sync.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 1824)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | Path to the SQLite database to sync. Default: the SDK-resolved canonical path (M3_DATABASE env / engine root / populated legacy store) — never a hardcoded repo-relative guess. | None |  | str |  |
| `--manifest` | Path to sync manifest YAML. Inferred from --db basename if omitted. | None |  | str |  |
| `--dry-run` | Print what would sync without touching either database. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config (chatlog_db_path)`
- `m3_halt (pid_is_alive)`
- `m3_sdk (M3Context, resolve_db_path)`
- `m3_sdk (resolve_warehouse_dsn)`
- `migrate_memory`
- `migrate_pg`
- `pg_fdw_sync (arrival_bound, lww_guard, version_guard)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `doctor.embed_space_probe (_family)`
- `m3_core.paths (reexec_in_project_venv)`
- `m3_core.secret_store (bump_secret_generation)`
- `memory.backends (active_backend)`
- `memory.backends (dialect)`
- `memory.backends.base (EmbeddingRow)`
- `memory.backends.dialect (chatlog_table_for)`
- `memory.backends.dialect (dialect_for)`
- `memory.backends.dialect (dialect_for_connection)`
- `memory.backends.postgres_backend (insert_embeddings_on)`
- `memory.backends.selector (backend_for)`
- `psycopg2.extras (execute_values)`
- `yaml`

---

## File dependencies (repo paths referenced)

- `Infer manifest path from db basename: config/sync_manifests/<stem>.yaml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
