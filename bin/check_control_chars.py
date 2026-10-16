#!/usr/bin/env python3
"""Detect stray control characters in text files — the PowerShell backtick trap.

In PowerShell the BACKTICK is the escape character, not the backslash. So
markdown inline-code written through a double-quoted PowerShell string is
silently mangled: the opening backtick escapes the word's first letter into a
control byte, and the closing backtick is consumed too.

    `bytemuck`  ->  \\x08ytemuck    (`b = backspace)
    `ring`      ->  \\r + "ing"     (`r = carriage return)
    `ndarray`   ->  \\n + "darray"  (`n = newline)
    `r2d2`      ->  \\r + "2d2"

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
\\x08 renders as nothing in most viewers and \\r/\\n just look like line breaks.

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
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# Control bytes that must never appear in a text file. TAB (\x09), LF (\x0a)
# and CR (\x0d) are legitimate; everything else in the C0 range is not. \x0b
# (vertical tab) and \x0c (form feed) are included because PowerShell's `v and
# `f produce them and neither has a legitimate use in our docs.
_BAD = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# PowerShell escape -> the letter it consumed, for the repair hint.
_PS_ESCAPES = {
    0x08: ("`b", "b"),
    0x0B: ("`v", "v"),
    0x0C: ("`f", "f"),
    0x07: ("`a", "a"),
    0x00: ("`0", "0"),
}

_TEXT_EXT = {".md", ".txt", ".rst", ".py", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh", ".ps1", ".sql"}


def _is_text(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _TEXT_EXT


class CheckError(Exception):
    """The check could not run; never to be read as a clean result."""


def _git(args: list[str]) -> bytes:
    try:
        r = subprocess.run(["git", *args], capture_output=True, check=False)
    except FileNotFoundError as e:
        raise CheckError(f"git not found on PATH ({e})") from e
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        raise CheckError(f"`git {' '.join(args)}` exited {r.returncode}: {err}")
    return r.stdout


def _staged_files() -> list[str]:
    """Text files staged for commit (added/copied/modified/renamed)."""
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]).decode("utf-8", "replace")
    return [p for p in out.split("\0") if p and _is_text(p)]


def _staged_blob(path: str) -> bytes:
    """The staged content of *path* -- what the commit will record."""
    return _git(["show", f":{path}"])


def scan(path: str) -> list[tuple[int, int, int]]:
    """Return [(line, col, byte)] for every stray control char in *path*.

    Raises OSError when the file cannot be read: an unread file is not clean.
    """
    with open(path, "rb") as fh:
        return scan_bytes(fh.read())


def scan_bytes(data: bytes) -> list[tuple[int, int, int]]:
    """Return [(line, col, byte)] for every stray control char in *data*."""
    hits = []
    for m in _BAD.finditer(data):
        off = m.start()
        line = data.count(b"\n", 0, off) + 1
        col = off - (data.rfind(b"\n", 0, off) + 1) + 1
        hits.append((line, col, data[off]))
    return hits


def _hint(byte: int, path: str, line: int) -> str:
    if byte in _PS_ESCAPES:
        seq, letter = _PS_ESCAPES[byte]
        return (
            f"looks like PowerShell consumed {seq!r} — a word starting with "
            f"{letter!r} lost its first letter (e.g. `{letter}ytemuck` -> "
            f"\\x{byte:02x}ytemuck). Restore the letter and the backticks."
        )
    return f"unexpected control byte \\x{byte:02x}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_control_chars.py",
        description="Fail on stray control characters in text files.",
    )
    ap.add_argument("paths", nargs="*", help="Files to scan.")
    ap.add_argument("--staged", action="store_true", help="Scan the git-staged text files instead of PATHS.")
    args = ap.parse_args(argv)

    try:
        targets = _staged_files() if args.staged else [p for p in args.paths if _is_text(p)]
    except CheckError as e:
        print(f"[control-chars] cannot list staged files: {e}", file=sys.stderr)
        return 2
    if not targets:
        return 0

    bad = 0
    unread: list[str] = []
    for path in targets:
        try:
            hits = scan_bytes(_staged_blob(path)) if args.staged else scan(path)
        except (OSError, CheckError) as e:
            unread.append(f"{path}: {e}")
            continue
        for line, col, byte in hits:
            bad += 1
            print(f"{path}:{line}:{col}: stray control char \\x{byte:02x} — {_hint(byte, path, line)}")

    if unread:
        print(f"[control-chars] {len(unread)} file(s) could not be read; not checked:", file=sys.stderr)
        for u in unread:
            print(f"  {u}", file=sys.stderr)
        if not bad:
            return 2

    if bad:
        print()
        print(f"[control-chars] {bad} stray control character(s) found.")
        print("  Cause: markdown backticks passed through a DOUBLE-quoted PowerShell")
        print("         string. PowerShell's escape char is ` (backtick), not \\.")
        print("  Fix:   restore the mangled words, and write files with a SINGLE-quoted")
        print("         here-string @'...'@ (no escape processing) or with Python.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
