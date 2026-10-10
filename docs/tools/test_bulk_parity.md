---
tool: bin/test_bulk_parity.py
sha1: f479e795af97
mtime_utc: 2026-10-10T21:05:16.585895+00:00
generated_utc: 2026-10-10T21:05:29.984715+00:00
private: false
---

# bin/test_bulk_parity.py

## Purpose

Real integration tests for memory_write_bulk_impl.

Verifies that bulk path actually invokes database operations and produces
equivalent memory_items rows to the single path, with proper enrichment,
variant handling, contradiction detection, and conversation emitters.

---

## Entry points

- `async def main()` (line 487)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (_cleanup)`
- `memory_core (memory_write_bulk_impl)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 102)
- `sqlite3.connect()  → `db_path`` (line 165)
- `sqlite3.connect()  → `db_path`` (line 212)
- `sqlite3.connect()  → `db_path`` (line 235)


---

## Notable external imports

- `m3_core.seams (is_windows)`
- `unittest.mock (AsyncMock, patch)`

---

## File dependencies (repo paths referenced)

- `test.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
