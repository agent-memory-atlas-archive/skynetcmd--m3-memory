---
tool: install_os.py
sha1: 89628eadda09
mtime_utc: 2026-10-16T23:02:58.010900+00:00
generated_utc: 2026-10-16T23:03:22.624534+00:00
private: false
---

# install_os.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 360)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `AGENT_OS_MASTER_KEY`
- `M3_INSTALL_OXIDATION`

---

## Calls INTO this repo (intra-repo imports)

- `m3_memory (_roots)`
- `m3_memory.rust_core_install (install_rust_core)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['fnm', '--version']`` (line 255)
- `subprocess.run()  → `['node', '--version']`` (line 217)
- `subprocess.run()  → `['nvm', 'version']`` (line 208)
- `subprocess.run()  → `['winget', '--version']`` (line 227)
- `subprocess.run()  → `[python_exe, pg_sync_script]`` (line 446)
- `subprocess.run()  → `cmd`` (line 106)
- `subprocess.run()` (line 228)


---

## Notable external imports

- `getpass`
- `venv`

---

## File dependencies (repo paths referenced)

- `requirements.txt`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
