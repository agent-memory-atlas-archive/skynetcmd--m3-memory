---
tool: bin/check_unreferenced.py
sha1: 85394247acfb
mtime_utc: 2026-10-14T03:47:29.831313+00:00
generated_utc: 2026-10-14T03:47:37.923781+00:00
private: false
---

# bin/check_unreferenced.py

## Purpose

Check that every unreferenced symbol is either removed or DECLARED.

vulture (at 100% confidence) lists what nothing in the code reads: parameters,
locals, unreachable branches, always-true/false conditions. That is a fact
about references, not a verdict: an unreferenced parameter can be a host
framework's signature, or a feature's reserved slot. So each finding is either
fixed, or declared below with its reason under one of two kinds:

  INTERFACE  the signature belongs to someone else (a framework, a C API)
  RESERVED   held for a planned feature that is not wired yet (never delete
             one to quiet this tool)

An undeclared finding fails, and a declaration that no longer matches a finding
fails too, so the lists cannot rot.

    python bin/check_unreferenced.py        # needs vulture (pip install vulture)

---

## Entry points

- `def main()` (line 63)
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

- `subprocess.run()  → `[sys.executable, '-m', 'vulture', '--version']`` (line 64)
- `subprocess.run()` (line 67)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
