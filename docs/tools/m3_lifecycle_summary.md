---
tool: bin/m3_lifecycle_summary.py
sha1: 5f1262176ad8
mtime_utc: 2026-10-10T21:05:16.900525+00:00
generated_utc: 2026-10-12T23:24:27.576781+00:00
private: false
---

# bin/m3_lifecycle_summary.py

## Purpose

CLI wrapper for the memory lifecycle/contradiction observability summary.

Thin operator-facing surface over ``memory_maintenance.memory_lifecycle_summary_impl``
— the SAME function the ``memory_lifecycle_summary`` MCP tool calls, so the agent
and the operator see identical numbers. Read-only.

    python bin/m3_lifecycle_summary.py                 # last 7 days, human table
    python bin/m3_lifecycle_summary.py --window-days 30 --json
    python bin/m3_lifecycle_summary.py --top-n 10

---

## Entry points

- `def main()` (line 51)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--window-days` | Look-back window in days (default 7). | `7` |  | int |  |
| `--top-n` | Rows in the most-revised/contradicted lists (0 = omit). | `5` |  | int |  |
| `--json` | Emit raw JSON instead of a human table. | `False` |  | store_true |  |
| `--database` | SQLite database path. Default: memory/agent_memory.db. | None |  | str |  |
| `--store` | Named store from m3_config.toml ('main' = the default). Not with --database. | None |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg)`
- `memory_maintenance`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
