---
tool: bin/setup_test_db.py
sha1: f2598b45bd4c
mtime_utc: 2026-10-13T01:42:49.859573+00:00
generated_utc: 2026-10-13T01:43:09.053015+00:00
private: false
---

# bin/setup_test_db.py

## Purpose

Seed a fresh SQLite DB with the full m3-memory schema for test isolation.

Brings the store to the current schema with m3's migration runner
(``migrate_memory.py up``), the same path an install takes, so the result can back
the live MCP server, the CLI scripts and the test suites.

Usage:
    python bin/setup_test_db.py --database memory/_test.db
    python bin/test_memory_bridge.py --database memory/_test.db

Exits non-zero if the migrations fail.

---

## Entry points

- `def main()` (line 30)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--force` | Wipe the target DB file before seeding (default: migrate the existing file). | `False` | Appends migrations to existing DB (idempotent). | store_true | Deletes DB + WAL/SHM files, then seeds fresh. |
| `--database` | SQLite database path. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |
| `--store` | Named store from m3_config.toml ('main' = the default). Not with --database. | None |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()` (line 52)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
