---
tool: bin/ai_mechanic.py
sha1: 15f47d2888b5
mtime_utc: 2026-10-10T16:52:23.463741+00:00
generated_utc: 2026-10-10T16:52:52.567725+00:00
private: false
---

# bin/ai_mechanic.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 84)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--force` | Required. Confirms you understand this drops tables. | `False` | Script refuses to run without the flag. | store_true | Permits DROP TABLE operations to recreate schema. |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Script refuses to run without the flag. | str | Routes destructive operations against PATH instead of default. |
| `--store` | Named store from m3_config.toml ('main' = the default). Not with --database. | None |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg)`
- `migrate_memory (MIGRATIONS_DIR)`
- `migrate_pg (_PG_MIGRATIONS_DIR)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.context (pointed_store_connection)`
- `m3_core.paths (resolve_cli_store, seam_backend, store_is_a_file)`

---

## File dependencies (repo paths referenced)

- `.down.sql`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
