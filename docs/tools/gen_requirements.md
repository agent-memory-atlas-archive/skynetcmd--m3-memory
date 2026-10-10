---
tool: bin/gen_requirements.py
sha1: 415bac3f01a2
mtime_utc: 2026-10-10T00:09:27.477356+00:00
generated_utc: 2026-10-10T00:11:32.076329+00:00
private: false
---

# bin/gen_requirements.py

## Purpose

Generate requirements.txt: the lane install set, resolved and pinned.

requirements.txt is the install path for machines without uv (pip-only lanes,
`pip install -r requirements.txt`). It is the project's dependencies plus the
`lint` and `test` dependency groups, resolved for every supported platform and
Python (`uv pip compile --universal`) with exact versions, so every lane
installs the same set. Do not edit it by hand.

It is resolved on its own, not exported from uv.lock: the lock also resolves
the optional extras, and crewai's caps would pin older shared packages
(pydantic 2.12.5 where `pip install m3-memory` gets 2.14.0) into every lane.

    python bin/gen_requirements.py           # after changing the declarations
    python bin/gen_requirements.py --upgrade # re-resolve to the newest allowed

tests/test_dependency_sources.py fails when pyproject and this file disagree.
Why each pin is what it is: docs/DEPENDENCY_NOTES.md.

---

## Entry points

- `def main()` (line 87)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--upgrade` | re-resolve to the newest allowed versions | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `cmd`` (line 78)


---

## Notable external imports

- `tomllib`

---

## File dependencies (repo paths referenced)

- `pyproject.toml`
- `requirements.txt`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
