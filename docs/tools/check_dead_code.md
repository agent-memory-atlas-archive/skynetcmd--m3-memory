---
tool: bin/check_dead_code.py
sha1: be7f81641ac3
mtime_utc: 2026-10-10T05:27:36.561481+00:00
generated_utc: 2026-10-10T05:28:18.147303+00:00
private: false
---

# bin/check_dead_code.py

## Purpose

Check for dead code with vulture: the one owner of the invocation.

Runs vulture at 100% confidence (unused variables and parameters, unreachable
code, always-true/false conditions), which is precise enough to gate on. Every
exception is keyed by (file, name) WITH its reason; an unlisted finding fails,
and an exception that no longer fires fails too, so the list cannot rot.

    python bin/check_dead_code.py        # needs vulture (pip install vulture)

---

## Entry points

- `def main()` (line 49)
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

- `subprocess.run()  → `[sys.executable, '-m', 'vulture', '--version']`` (line 50)
- `subprocess.run()` (line 55)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
