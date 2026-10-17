---
tool: bin/chatlog_status.py
sha1: b9b4d2c7e49a
mtime_utc: 2026-10-17T17:02:52.755614+00:00
generated_utc: 2026-10-17T17:03:03.398339+00:00
private: false
---

# bin/chatlog_status.py

## Purpose

chatlog_status.py — single-call summary of the chat log subsystem state.

Exports:
- chatlog_status_impl() -> str : returns JSON summary
- CLI: python bin/chatlog_status.py [--json]

Returns row counts from SQLite; everything else from state file + config.
Cold call <50ms (no full table scans).

---

## Entry points

- `def main()` (line 1082)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--json` | Output JSON format | `False` |  | store_true |  |
| `--live` | Run live status monitor | `False` |  | store_true |  |
| `-i`, `--interval` | Refresh interval for live monitor in seconds (default: 5.0) | `5.0` |  | float |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `chatlog_config (_minutes_since, capture_idle_minutes)`
- `m3_sdk (resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `cmd`` (line 765)

**sqlite**

- `sqlite3.connect()  → `chatlog_db`` (line 138)
- `sqlite3.connect()  → `files_db`` (line 208)
- `sqlite3.connect()  → `main_db`` (line 126)
- `sqlite3.connect()  → `uri`` (line 306)


---

## Notable external imports

- `doctor (environment_probe)`
- `files_memory.config (files_table)`
- `files_memory.db (_db)`
- `files_memory.db (_files_store_is_a_schema)`
- `m3_core.paths (configured_files_db, resolve_engine_file)`
- `m3_core.paths (seam_backend)`
- `m3_core.seams (childenv)`
- `m3_core.seams (read_key)`
- `m3_core.seams (wait_for_any_key)`
- `memory (doctor)`
- `memory.backends (resolve_backend_name)`
- `memory.backends.sqlite_backend (SqliteDialect)`
- `memory.config (FILES_DB_PATH)`

---

## File dependencies (repo paths referenced)

- `.db`
- `files_database.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
