---
tool: bin/session_handoff.py
sha1: ef559029b396
mtime_utc: 2026-10-08T22:11:14.607460+00:00
generated_utc: 2026-10-08T22:11:48.281718+00:00
private: false
---

# bin/session_handoff.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (M3Context)`
- `mcp_compat (FastMCP)`
- `memory_core`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `memory.backends (active_backend)`
- `memory.backends (dialect)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
