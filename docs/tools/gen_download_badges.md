---
tool: bin/gen_download_badges.py
sha1: faa544a80ed5
mtime_utc: 2026-10-14T02:24:24.344631+00:00
generated_utc: 2026-10-14T02:24:34.870498+00:00
private: false
---

# bin/gen_download_badges.py

## Purpose

Generate download-count badge data (PyPI + GitHub) for the README.

Writes two shields.io *endpoint* JSON files that the README badges point at:

  * docs/badges/pypi-downloads.json  — estimated TOTAL PyPI downloads
        (pypistats "overall", WITHOUT mirrors — excludes bandersnatch/CI mirror
        bots, so it approximates real installs).
  * docs/badges/github-clones.json   — TOTAL unique GitHub clones
        (the repo traffic API's daily `uniques`).

Both sources keep only a rolling window (pypistats ~180 days, GitHub 14 days), so
the totals are RUNNING totals. The public repo keeps only the aggregate:
docs/badges/counters.json holds each source's total and the last complete day it
counted, and each run adds the complete days after that (a weekly run stays inside
both windows; a gap longer than a window is reported, since those days are gone).
The per-day history is private (user decision 2026-10-13): `--history-dir` (for
example ~/.m3-private/badges) also merges every day into pypi-history.json and
clone-history.json there. CI never passes it, so no history reaches this repo.

These are ESTIMATES by design — download/clone counts include automation and
can't be deduplicated to people; the numbers are labelled accordingly and the
mirror-excluded / unique variants are chosen to be the least-noisy signal.

The README references the JSON via a shields endpoint URL, e.g.:
    https://img.shields.io/endpoint?url=<raw json url>&style=flat-square
so shields renders the current committed number with no third-party number source.

Run in CI on a schedule; commit the result back. Locally, keeping the history:
    GITHUB_TOKEN=$(gh auth token) python bin/gen_download_badges.py         --repo skynetcmd/m3-memory --package m3-memory --history-dir ~/.m3-private/badges

Exit codes: 0 = badges written (or unchanged); 2 = hard error (network/auth).
Standard library only.

---

## Entry points

- `def main()` (line 255)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--repo` | owner/name | `skynetcmd/m3-memory` |  | str |  |
| `--package` | PyPI package name | `m3-memory` |  | str |  |
| `--badges-dir` | output directory | `docs/badges` |  | str |  |
| `--history-dir` | private directory for the per-day history (e.g. ~/.m3-private/badges); never the repo | None |  | str |  |

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
- `pypi-downloads.json`
- `pypi-history.json`
- `pypi-monthly.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
