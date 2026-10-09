---
tool: bin/check_env_access.py
sha1: e7fee31bbce2
mtime_utc: 2026-10-09T00:51:11.605442+00:00
generated_utc: 2026-10-09T00:53:32.483720+00:00
private: false
---

# bin/check_env_access.py

## Purpose

Inventory every process-environment access in m3's production code (plan Step 2A.1).

The configuration redesign (CONFIG_DESIGN) moves every m3 setting behind ONE
resolver. Until then m3 reads and writes the environment from hundreds of
places, in more shapes than a grep finds: aliased modules (``import os as
_os``), ``from os import environ``, splat copies (``{**os.environ}``,
``dict(os.environ)``), ``subprocess.run(env=os.environ)``, ``load_dotenv()``,
dynamic names (``os.getenv(name)``), and wrappers such as ``getenv_compat``.
This gate walks the AST so each shape is counted, and so a planted violation
of each one is caught (``--selftest``).

DENY BY DEFAULT. A site is a violation unless it is in an allowed module (the
config package, once it exists, and the ``child_env`` helper) or reads a non-m3
OS name on the allow-list (HOME, PATH, ...). Writes, whole-environment copies
and dynamic names are violations wherever an allowed module is not.

REPORT MODE (this step): print the inventory and exit 0, so the baseline can be
reviewed before the gate enforces anything. A file that does not parse is NOT
skipped silently -- it is reported and makes the run exit 2, because an
unscanned file is not a clean file (the same rule CI applies to bandit).

Usage:
    python bin/check_env_access.py                 # report: findings + summary
    python bin/check_env_access.py --json          # machine-readable report
    python bin/check_env_access.py --summary       # counts only
    python bin/check_env_access.py --selftest      # planted violations must all be caught

Exit: 0 = report written (or selftest passed); 1 = selftest failed;
2 = a scanned file could not be parsed.

---

## Entry points

- `def main()` (line 359)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--selftest` | every planted violation must be caught | `False` |  | store_true |  |
| `--json` | emit the full report as JSON | `False` |  | store_true |  |
| `--summary` | print counts only | `False` |  | store_true |  |

---

## Environment variables read

- `HOME`
- `M3_X`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

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
