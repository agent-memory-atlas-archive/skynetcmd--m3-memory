---
tool: bin/check_control_chars.py
sha1: 0c31e9b3eeae
mtime_utc: 2026-10-16T17:35:43.426979+00:00
generated_utc: 2026-10-16T19:03:59.104387+00:00
private: false
---

# bin/check_control_chars.py

## Purpose

Detect stray control characters in text files — the PowerShell backtick trap.

In PowerShell the BACKTICK is the escape character, not the backslash. So
markdown inline-code written through a double-quoted PowerShell string is
silently mangled: the opening backtick escapes the word's first letter into a
control byte, and the closing backtick is consumed too.

    `bytemuck`  ->  \x08ytemuck    (`b = backspace)
    `ring`      ->  \r + "ing"     (`r = carriage return)
    `ndarray`   ->  \n + "darray"  (`n = newline)
    `r2d2`      ->  \r + "2d2"

Only words starting with b, n, r, t, a, f, v, or 0 are affected — exactly
PowerShell's single-letter escapes — which is why `sqlx`, `sha2`, `proptest`
and `maturin` all survive untouched in the same document. That selectivity
makes the damage easy to miss in review: most of the file looks perfect.

What this DETECTS is narrower: `b, `a, `f, `v and `0 (and any other C0 byte).
`n, `r and `t produce LF, CR and TAB, which are legitimate in text files, so
those three manglings are not detectable here.

Real incident (2026-07-15, repaired 2026-07-23): a design plan was authored
through such a string. Six crate names lost their leading letter and the file
was committed twice with the damage before anyone noticed, because a stray
\x08 renders as nothing in most viewers and \r/\n just look like line breaks.

Fix when writing files from PowerShell: use a SINGLE-quoted here-string
(``@'...'@``), which does no escape processing — never ``@"..."@``. Better
still, write the file with Python or an editor rather than shell string
interpolation.

Usage:
    python bin/check_control_chars.py [paths...]   # explicit paths
    python bin/check_control_chars.py --staged     # git-staged text files

``--staged`` scans the STAGED blob (what the commit will contain), not the
working-tree file.

Exit 0 = clean, 1 = corruption found (prints file:line:col and a repair hint),
2 = could not check (git failed, a file could not be read).

---

## Entry points

- `def main()` (line 131)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `paths` | Files to scan. | — |  | str |  |
| `--staged` | Scan the git-staged text files instead of PATHS. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['git', *args]`` (line 80)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `.ini`
- `.json`
- `.md`
- `.sh`
- `.sql`
- `.toml`
- `.txt`
- `.yaml`
- `.yml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
