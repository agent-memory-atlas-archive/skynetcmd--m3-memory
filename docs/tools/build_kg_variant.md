---
tool: bin/build_kg_variant.py
sha1: 6b029d60d0ca
mtime_utc: 2026-10-10T16:50:49.966686+00:00
generated_utc: 2026-10-10T16:50:50.314845+00:00
private: false
---

# bin/build_kg_variant.py

## Purpose

Build a KG-enriched variant from an existing source variant.

Duplicates memory_items + memory_embeddings under a new variant name (fresh IDs),
then populates memory_relationships with `related` edges computed from cosine
similarity on the duplicated embeddings. No LLM calls, no re-ingest.

Usage:
    python bin/build_kg_variant.py         --source-variant LME-ingestion         --target-variant LME-kg-sparse         --top-n 3 --sim-threshold 0.80

    python bin/build_kg_variant.py         --source-variant LME-ingestion         --target-variant LME-kg-dense         --top-n 8 --sim-threshold 0.70

Every backend: SQLite takes --database (default: the active store); PostgreSQL
uses the configured database and refuses a path.

---

## Entry points

- `def main()` (line 183)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--source-variant` |  | — | Required; fails if not provided | str | Variant to duplicate items/embeddings from |
| `--target-variant` |  | — | Required; fails if not provided | str | New variant name for duplicated items and KG edges |
| `--top-n` |  | — | Required; fails if not provided | int | Number of most similar items to link per item |
| `--sim-threshold` |  | — | Required; fails if not provided | float | Minimum cosine similarity to create an edge |
| `--wipe-target` | Delete any existing items/edges under target variant before building | `False` | Fails if target variant has existing items | store_true | Deletes all items/edges in target before duplicating source |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes this run against PATH for all DB reads/writes. |
| `--store` | Named store from m3_config.toml ('main' = the default). Not with --database. | None |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, getenv_compat)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.context (pointed_store_connection)`
- `m3_core.paths (resolve_cli_store, seam_dialect)`
- `numpy`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
