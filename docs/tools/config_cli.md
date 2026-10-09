---
tool: bin/config_cli.py
sha1: b5889d8426f9
mtime_utc: 2026-10-09T04:12:24.451753+00:00
generated_utc: 2026-10-09T04:13:53.661113+00:00
private: false
---

# bin/config_cli.py

## Purpose

`m3 config show`: every setting, its value and where it came from (read-only).

    m3 config show [--origin] [--json] [--config FILE]

A repair command: it always starts. An invalid m3_config.toml or environment
value is printed in full (every problem, its line and the fix) with exit 2,
never a traceback (CONFIG_DESIGN §3). Secrets are listed by name and source
only; a value is never printed (§10).

Exit codes: 0 = shown; 2 = the configuration is invalid (problems printed).

---

## Entry points

- `def main()` (line 90)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--origin` | say where each value came from | `False` |  | store_true |  |
| `--json` | machine-readable output | `False` |  | store_true |  |
| `--config` | read this file instead of the located one | — |  | str |  |

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
- `m3_core.config.loader (ConfigFileError)`
- `m3_core.config.resolve (ConfigEnvErrors)`
- `m3_core.config.settings (Settings, build)`
- `m3_core.secret_store (SecretsFileError, secrets_file_path)`
- `m3_core.secret_store (explain_named_secret)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
