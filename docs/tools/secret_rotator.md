---
tool: bin/secret_rotator.py
sha1: b8fd6ac8721b
mtime_utc: 2026-10-10T05:41:20.163029+00:00
generated_utc: 2026-10-10T05:42:31.752034+00:00
private: false
---

# bin/secret_rotator.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dry-run` | Show planned rotations without writing | `False` | Rotates secrets: backs up old value, generates new token, encrypts to vault, logs event. | store_true | Logs planned rotations (new token length) but skips backup, encryption, vault write, and event logging. |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |
| `--store` | Named store from m3_config.toml ('main' = the default). Not with --database. | None |  | str |  |

---

## Environment variables read

- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (add_log_file_arg, setup_task_runtime)`
- `auth_utils (set_api_key)`
- `m3_sdk (M3Context)`
- `m3_sdk (add_database_arg)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.paths (require_sqlite_backend)`
- `secrets`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
