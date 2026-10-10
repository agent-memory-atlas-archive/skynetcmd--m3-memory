---
tool: bin/pg_setup.py
sha1: 9ce5d31020f5
mtime_utc: 2026-10-10T06:50:41.184999+00:00
generated_utc: 2026-10-10T06:50:42.360696+00:00
private: false
---

# bin/pg_setup.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 102)
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

- `m3_core.seams (is_windows)`
- `m3_core.secret_store (PgRole, resolve_pg_dsn)`
- `psycopg2`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
