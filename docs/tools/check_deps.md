---
tool: bin/check_deps.py
sha1: 06195dbefdc6
mtime_utc: 2026-10-17T11:50:41.956976+00:00
generated_utc: 2026-10-17T11:51:00.059286+00:00
private: false
---

# bin/check_deps.py

## Purpose

Check declared vs imported dependencies with deptry: the one owner of the invocation.

bin/ is a flat directory of first-party scripts and packages imported by bare
name, which deptry cannot infer, so the first-party list is computed here from
the tree (a hand-kept list would fail on every new script). Every exception is
listed below WITH its reason; an unlisted finding fails.

    python bin/check_deps.py        # needs deptry (pip install deptry)

CI runs it after installing requirements.txt, so deptry can map installed
distributions to import names (yaml -> PyYAML, PIL -> pillow, ...).

---

## Entry points

- `def main()` (line 70)
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

- `subprocess.run()  → `[sys.executable, '-m', 'deptry', '--version']`` (line 73)
- `subprocess.run()  → `cmd`` (line 90)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
