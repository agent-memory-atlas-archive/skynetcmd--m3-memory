---
tool: bin/gen_download_badges.py
sha1: 4b266b17c0c6
mtime_utc: 2026-10-17T05:51:47.175107+00:00
generated_utc: 2026-10-17T05:51:47.796058+00:00
private: false
---

# bin/gen_download_badges.py

## Purpose

Generate download-count badge data (PyPI + GitHub) for the README.

Writes two shields.io *endpoint* JSON files that the README badges point at:

  * docs/badges/pypi-downloads.json / pypi-monthly.json — PyPI downloads, total
        and last 30 days, WITH mirrors (pypistats "overall"), labelled "PyPI
        downloads" as pepy.tech and most projects' badges are. The totals WITHOUT
        mirrors (closer to real installs) are kept in counters.json beside them.
  * docs/badges/github-clones.json   — TOTAL unique GitHub clones
        (the repo traffic API's daily `uniques`).

Both sources keep only a rolling window (pypistats ~180 days, GitHub 14 days), so
the totals are RUNNING totals. The public repo keeps only the aggregate:
docs/badges/counters.json holds each source's total and the last complete day it
counted, and each run adds the complete days after that (a weekly run stays inside
both windows; a gap longer than a window is reported, since those days are gone).
The per-day history stays out of the repo: `--history-dir` (a private directory
of your choice) also merges every day into pypi-history.json and
clone-history.json there. CI never passes it, so no history reaches this repo.

These are ESTIMATES by design — download/clone counts include automation and
can't be deduplicated to people; the numbers are labelled accordingly and the
mirror-excluded / unique variants are chosen to be the least-noisy signal.

The README references the JSON via a shields endpoint URL, e.g.:
    https://img.shields.io/endpoint?url=<raw json url>&style=flat-square
so shields renders the current committed number with no third-party number source.

Run in CI on a schedule; commit the result back. Locally, keeping the history:
    GITHUB_TOKEN=$(gh auth token) python bin/gen_download_badges.py         --repo skynetcmd/m3-memory --package m3-memory --history-dir ~/badge-history

Exit codes: 0 = badges written (or unchanged); 2 = hard error (network/auth).
Standard library only.

---

## Entry points

- `def main()` (line 256)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--repo` | owner/name | `skynetcmd/m3-memory` |  | str |  |
| `--package` | PyPI package name | `m3-memory` |  | str |  |
| `--badges-dir` | output directory | `docs/badges` |  | str |  |
| `--history-dir` | private directory for the per-day history (any directory outside the repo); never the repo | None |  | str |  |

---

## Environment variables read

- `GITHUB_TOKEN`
- `M3_STARGAZER`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `clone-history.json`
- `counters.json`
- `github-clones.json`
- `pypi-all-history.json`
- `pypi-history.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
