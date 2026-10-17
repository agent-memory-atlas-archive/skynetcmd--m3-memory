---
tool: bin/config_cli.py
sha1: 4164a9b39418
mtime_utc: 2026-10-17T04:54:20.195697+00:00
generated_utc: 2026-10-17T04:54:46.951033+00:00
private: false
---

# bin/config_cli.py

## Purpose

`m3 config`: show, read and change m3_config.toml.

    m3 config show [--origin] [--json] [--config FILE]
    m3 config get KEY [--origin] [--config FILE]
    m3 config set KEY VALUE [--yes] [--config FILE]
    m3 config unset KEY [--yes] [--config FILE]
    m3 config import-env [--dry-run] [--non-interactive] [--config FILE]

`set` takes a value as the environment would spell it (a list comma-separated)
and writes it in its TOML type under the file's lock. Secrets
are never written to this file. A key that moves the store or the roots asks a
person at the console, or needs --yes (plan "Config-set confirmation": defence in
depth, not a boundary; a shell can edit the file directly).

A repair command: it always starts. An invalid m3_config.toml or environment
value is printed in full (every problem, its line and the fix) with exit 2,
never a traceback. Secrets are listed by name and source
only; a value is never printed (§10).

Exit codes: 0 = done; 1 = declined at the prompt; 2 = refused or invalid
(problems printed).

---

## Entry points

- `def main()` (line 248)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--origin` | say where each value came from | `False` |  | store_true |  |
| `--json` | machine-readable output | `False` |  | store_true |  |
| `key` |  | — |  | str |  |
| `--origin` | say where the value came from | `False` |  | store_true |  |
| `key` |  | — |  | str |  |
| `value` | as the environment would spell it; a list comma-separated | — |  | str |  |
| `key` |  | — |  | str |  |
| `--dry-run` | say what would be imported; write nothing | `False` |  | store_true |  |
| `--non-interactive` | never ask; list each disagreement | `False` |  | store_true |  |
| `--yes`, `-y` | skip the confirmation for a sensitive key | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_memory.install (import_env)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `importlib`
- `m3_core.config (REGISTRY, KeyClass, format_config_error, locate)`
- `m3_core.config.keys (DB_BACKEND)`
- `m3_core.config.keys (RETIRED)`
- `m3_core.config.loader (ConfigFileError)`
- `m3_core.config.registry (ConfigValueError, Key, Scope)`
- `m3_core.config.resolve (ConfigEnvErrors)`
- `m3_core.config.settings (Settings, build, env_contradicting, with_flags)`
- `m3_core.config.writer (ConfigWriteError, set_values)`
- `m3_core.paths (add_config_arg)`
- `m3_core.seams (console_has_person)`
- `m3_core.secret_store (SecretsFileError, secrets_file_path)`
- `m3_core.secret_store (explain_named_secret)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
