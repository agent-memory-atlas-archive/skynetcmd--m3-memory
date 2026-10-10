---
tool: bin/thermal_utils.py
sha1: 3a3078fda460
mtime_utc: 2026-10-10T21:05:16.652430+00:00
generated_utc: 2026-10-10T21:05:30.019478+00:00
private: false
---

# bin/thermal_utils.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (no_window_kwargs)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['sysctl', '-n', 'kern.thermal_pressure']`` (line 95)
- `subprocess.run()` (line 113)
- `subprocess.run()` (line 141)


---

## Notable external imports

- `m3_core.seams (is_windows, os_name)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
