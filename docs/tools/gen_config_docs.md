---
tool: bin/gen_config_docs.py
sha1: 8d8d0691c5e0
mtime_utc: 2026-10-16T23:48:33.518969+00:00
generated_utc: 2026-10-16T23:48:39.749306+00:00
private: false
---

# bin/gen_config_docs.py

## Purpose

Generate m3's configuration docs and the shipped default config from the registry.

    python bin/gen_config_docs.py

Writes, all from ``m3_core.config.REGISTRY`` (never hand-edited):
  * docs/CONFIG_REFERENCE.md and docs/schema/m3_config.schema.json;
  * bin/m3_core/config/default-m3_config.toml, the file m3 copies into a config
    root that has no m3_config.toml (``m3_core.config.default_file`` renders it),
    and its SHA-256 in bin/m3_core/config/_default_config_sha256.py, which m3
    checks before copying;
  * docs/ENVIRONMENT_VARIABLES.md: the environment names m3 still reads by
    design, every legacy name of a setting with the key it moved to, and the
    retired names.
tests/test_generated_docs_fresh.py fails when the registry moves without them.

---

## Entry points

- `def main()` (line 309)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `check_env_access`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.atomic_io (atomic_write)`
- `m3_core.config (REGISTRY, KeyClass, default_file)`
- `m3_core.config.keys (NOT_SETTINGS, RETIRED)`
- `m3_core.config.loader (CONFIG_FILE_NAME, SCHEMA_VERSION, show_key)`
- `m3_core.config.registry (Key, Scope, choices_of, type_name)`
- `m3_core.config.stores (LEGACY_LOCATORS)`

---

## File dependencies (repo paths referenced)

- `CONFIG_REFERENCE.md`
- `ENVIRONMENT_VARIABLES.md`
- `m3_config.schema.json`
- `m3_config.toml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
