---
tool: bin/re_embed_all.py
sha1: 331a35e25a27
mtime_utc: 2026-10-10T21:05:16.456797+00:00
generated_utc: 2026-10-10T21:05:29.911301+00:00
private: false
---

# bin/re_embed_all.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 70)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes this run against PATH for all DB reads/writes. |
| `--store` | Named store from m3_config.toml ('main' = the default). Not with --database. | None |  | str |  |

---

## Environment variables read

- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`
- `memory_core (_embed, _pack)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.paths (active_database, seam_backend, seam_dialect)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
