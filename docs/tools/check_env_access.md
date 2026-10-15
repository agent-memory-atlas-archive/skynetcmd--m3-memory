---
tool: bin/check_env_access.py
sha1: ee2f8a32aaa8
mtime_utc: 2026-10-15T16:38:09.170099+00:00
generated_utc: 2026-10-15T16:38:26.599091+00:00
private: false
---

# bin/check_env_access.py

## Purpose

Inventory every process-environment access in m3's production code.

The configuration redesign moves every m3 setting behind ONE
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

OWNERS: a site whose JOB is reading
the environment -- an approved locator (the roots), the legacy-name shim, the
doctor / import-env scanners, the parity snapshot -- carries ``# env-owner: <why>``
on its line or alone on the line above. Its verdict is ``owner``, not
``violation``; owners have their own ratchet, so a new one is still reviewed. A
marker without a reason is ignored.

REPORT MODE: print the inventory and exit 0. The counts are a RATCHET
(tests/test_env_access_ratchet.py): they may only fall, and a
fall must lower the stored budget. ``--tests`` counts the other side, env WRITES
of non-OS names in tests/ (tests that configure m3 through the environment
instead of the ``m3_settings`` fixture). A file that does not parse is NOT
skipped silently -- it is reported and makes the run exit 2, because an
unscanned file is not a clean file (the same rule CI applies to bandit).

Usage:
    python bin/check_env_access.py                 # report: findings + summary
    python bin/check_env_access.py --json          # machine-readable report
    python bin/check_env_access.py --summary       # counts only
    python bin/check_env_access.py --tests         # env writes in tests/
    python bin/check_env_access.py --selftest      # planted violations must all be caught

Exit: 0 = report written (or selftest passed); 1 = selftest failed;
2 = a scanned file could not be parsed.

---

## Entry points

- `def main()` (line 703)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--selftest` | every planted violation must be caught | `False` |  | store_true |  |
| `--json` | emit the full report as JSON | `False` |  | store_true |  |
| `--summary` | print counts only | `False` |  | store_true |  |
| `--tests` | inventory env WRITES of non-OS names in tests/ instead of production reads | `False` |  | store_true |  |

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
