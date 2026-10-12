---
tool: bin/config_cli.py
sha1: 7f8fcf1b7043
mtime_utc: 2026-10-12T12:15:02.893715+00:00
generated_utc: 2026-10-12T12:15:03.566746+00:00
private: false
---

# bin/config_cli.py

## Purpose

`m3 config`: show, read and change m3_config.toml.

    m3 config show [--origin] [--json] [--config FILE]
    m3 config get KEY [--origin] [--config FILE]
    m3 config set KEY VALUE [--yes] [--config FILE]
    m3 config unset KEY [--yes] [--config FILE]

`set` takes a value as the environment would spell it (a list comma-separated)
and writes it in its TOML type under the file's lock (CONFIG_DESIGN §12). Secrets
are never written to this file. A key that moves the store or the roots asks a
person at the console, or needs --yes (plan "Config-set confirmation": defence in
depth, not a boundary; a shell can edit the file directly).

A repair command: it always starts. An invalid m3_config.toml or environment
value is printed in full (every problem, its line and the fix) with exit 2,
never a traceback (CONFIG_DESIGN §3). Secrets are listed by name and source
only; a value is never printed (§10).

Exit codes: 0 = done; 1 = declined at the prompt; 2 = refused or invalid
(problems printed).

---

## Entry points

- `def main()` (line 220)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--origin` | say where each value came from | `False` |  | store_true |  |
| `--json` | machine-readable output | `False` |  | store_true |  |
| `--config` | read this file instead of the located one | — |  | str |  |
| `key` |  | — |  | str |  |
| `--origin` | say where the value came from | `False` |  | store_true |  |
| `key` |  | — |  | str |  |
| `value` | as the environment would spell it; a list comma-separated | — |  | str |  |
| `key` |  | — |  | str |  |
| `--yes`, `-y` | skip the confirmation for a sensitive key | `False` |  | store_true |  |
| `--config` | use this file instead of the located one | — |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.config (REGISTRY, KeyClass, format_config_error, locate)`
- `m3_core.config.keys (RETIRED)`
- `m3_core.config.loader (ConfigFileError)`
- `m3_core.config.registry (ConfigValueError, Key)`
- `m3_core.config.resolve (ConfigEnvErrors, env_outranks_file)`
- `m3_core.config.settings (Settings, build, env_contradicting)`
- `m3_core.config.writer (ConfigWriteError, set_values)`
- `m3_core.seams (console_has_person)`
- `m3_core.secret_store (SecretsFileError, secrets_file_path)`
- `m3_core.secret_store (explain_named_secret)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
