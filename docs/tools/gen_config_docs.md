---
tool: bin/gen_config_docs.py
sha1: 15bc542d82c4
mtime_utc: 2026-10-12T11:50:44.018293+00:00
generated_utc: 2026-10-12T12:09:03.290307+00:00
private: false
---

# bin/gen_config_docs.py

## Purpose

Generate the m3_config.toml reference and JSON Schema from the config registry.

    python bin/gen_config_docs.py

Writes docs/CONFIG_REFERENCE.md and docs/schema/m3_config.schema.json. Both are
derived from ``m3_core.config.REGISTRY`` and never hand-edited;
tests/test_generated_docs_fresh.py fails when the registry moves without them.

---

## Entry points

- `def main()` (line 131)
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
- `m3_core.config (REGISTRY, KeyClass)`
- `m3_core.config.loader (CONFIG_FILE_NAME, SCHEMA_VERSION)`
- `m3_core.config.registry (Key, choices_of, type_name)`

---

## File dependencies (repo paths referenced)

- `CONFIG_REFERENCE.md`
- `m3_config.schema.json`
- `m3_config.toml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
