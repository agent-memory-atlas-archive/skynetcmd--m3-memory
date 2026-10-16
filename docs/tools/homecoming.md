---
tool: bin/homecoming.py
sha1: 42af369541e4
mtime_utc: 2026-10-16T17:38:53.876096+00:00
generated_utc: 2026-10-16T19:03:59.297753+00:00
private: false
---

# bin/homecoming.py

## Purpose

bin/homecoming.py — "Homecoming" migration script for m3-memory.
Relocates repo-relative and old ~/.m3-memory/ state to new decoupled standard roots
(~/.m3/config and ~/.m3/engine).

This tool is non-destructive: it COPIES databases using the SQLite Backup API
and COPIES configuration files; the legacy originals are left in place. It does NOT modify system-wide tool settings
(Claude/Gemini) to ensure safety. manually update tool settings if needed.

---

## Entry points

- `def main()` (line 104)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (get_m3_config_root, get_m3_engine_root)`
- `sqlite_snapshot (SqliteSnapshotError, snapshot_sqlite)`
- `sqlite_snapshot (file_row_counts)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.atomic_io (atomic_write)`
- `m3_core.seams (root_migration)`

---

## File dependencies (repo paths referenced)

- `.db`
- `memory/.chatlog_config.json`
- `memory/.chatlog_ingest_cursor.json`
- `memory/.chatlog_state.json`
- `memory/.migrate_config.json`
- `memory/agent_chatlog.db`
- `memory/agent_memory.db`
- `memory/agent_test_bench.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
