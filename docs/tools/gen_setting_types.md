---
tool: bin/gen_setting_types.py
sha1: 0bd7bc172fa4
mtime_utc: 2026-10-11T20:18:18.885035+00:00
generated_utc: 2026-10-11T20:19:14.713731+00:00
private: false
---

# bin/gen_setting_types.py

## Purpose

Generate m3_memory/_setting_types.py: the static type of ``setting(name)``.

``settings.setting(name)`` returns whatever the registry key holds, so its declared
return type is the union of every value type, and each converted call site lost
its type (190 mypy errors at 2026-10-11). The registry already knows each key's
parser and default; this turns that into ``Literal`` overloads, one per
(type, nullable) group, so ``setting("search.row_cap")`` is ``int`` with no call
site changed. The generated module is stdlib-only, so both accessors take their
signature from it: ``m3_core.config.settings.setting`` and the installed package's
``m3_memory._config.setting``. tests/test_generated_docs_fresh.py fails when it
drifts from the registry.

Usage: python bin/gen_setting_types.py

---

## Entry points

- `def main()` (line 118)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

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

- `m3_core.atomic_io (atomic_write)`
- `m3_core.config (REGISTRY)`
- `m3_core.config.registry (Key, known_type)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
