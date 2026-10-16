---
tool: bin/deep_sync.py
sha1: b8861652d84f
mtime_utc: 2026-10-16T17:39:41.153264+00:00
generated_utc: 2026-10-16T19:03:59.215096+00:00
private: false
---

# bin/deep_sync.py

## Purpose

deep_sync.py — commit and push an EXPLICIT list of paths in this checkout.

Housekeeping (bin/cleanup_logs.sh when present), then stage only the paths
named on the command line, commit them if anything changed, and push to the
remote and branch named on the command line. There are no defaults for any of
the three: the old form ran ``git add .`` over the whole payload and pushed to
``origin main``, and in an m3-memory checkout ``origin`` is the PUBLIC repo.

Exit codes: 0 committed and pushed, or nothing changed in the named paths;
1 any git step failed (its stderr is logged).

Usage:
    python bin/deep_sync.py --remote private --branch main reports/ notes.md

---

## Entry points

- `def main()` (line 97)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `paths` | paths to stage (relative to the checkout); nothing else is staged | — |  | str |  |
| `--remote` | remote to push to (check it is the one you mean) | — |  | str |  |
| `--branch` | remote branch to push HEAD to | — |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['git', '-C', WORKSPACE, *args]`` (line 46)
- `subprocess.run()  → `[cleanup_script]`` (line 65)


---

## Notable external imports

- `m3_core.seams (hidden_window_kwargs)`

---

## File dependencies (repo paths referenced)

- `cleanup_logs.sh`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
