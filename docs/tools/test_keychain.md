---
tool: bin/test_keychain.py
sha1: 1ec7d4184e10
mtime_utc: 2026-10-10T06:49:09.067844+00:00
generated_utc: 2026-10-10T06:49:17.107141+00:00
private: false
---

# bin/test_keychain.py

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

- `auth_utils (get_api_key)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.seams (is_windows)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
