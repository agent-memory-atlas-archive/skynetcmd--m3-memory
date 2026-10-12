---
tool: bin/re_embed_all.py
sha1: c1a768d7e509
mtime_utc: 2026-10-12T22:51:17.782220+00:00
generated_utc: 2026-10-12T23:01:48.347732+00:00
private: false
---

# bin/re_embed_all.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 62)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes this run against PATH for all DB reads/writes. |
| `--store` | Named store from m3_config.toml ('main' = the default). Not with --database. | None |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`
- `memory_core`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.paths (active_database, seam_backend)`
- `m3_core.paths (pin_database)`
- `memory.write (rewindow_memory)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
