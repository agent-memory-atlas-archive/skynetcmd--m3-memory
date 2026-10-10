---
tool: bin/deep_sync.py
sha1: fb5870443d0e
mtime_utc: 2026-10-10T21:05:17.248881+00:00
generated_utc: 2026-10-10T21:05:29.482911+00:00
private: false
---

# bin/deep_sync.py

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

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.check_call()  → `['git', '-C', WORKSPACE, *args]`` (line 18)
- `subprocess.run()  → `[cleanup_script]`` (line 31)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `cleanup_logs.sh`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
