---
tool: bin/secret_rotator.py
sha1: 9e562ed85029
mtime_utc: 2026-10-12T22:51:17.785729+00:00
generated_utc: 2026-10-12T23:24:27.788602+00:00
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
| `--database` | SQLite database path. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |
| `--store` | Named store from m3_config.toml ('main' = the default). Not with --database. | None |  | str |  |

---

## Environment variables read

_(none detected)_

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

- `m3_core.paths (active_database, seam_backend, seam_dialect)`
- `m3_core.paths (apply_early_argv)`
- `m3_core.paths (pin_database)`
- `secrets`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
