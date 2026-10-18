"""Scan a push diff's ADDED lines for prohibited patterns. Fails CLOSED.

Each pattern is compiled INDEPENDENTLY. A policy joined into one expression
takes the whole policy down with its weakest line, and a scanner that cannot
compile its patterns must not be mistaken for one that found nothing — so
anything meaning "the policy was not evaluated" blocks the push: an unreadable
file, an empty list, any pattern that will not compile, an uncompilable
allow-list. Python's ``re`` also keeps one meaning across all three supported
platforms rather than deferring to each one's ``grep``.

Pattern text is never printed. The patterns enumerate what we treat as
sensitive, so diagnostics carry the file, the 1-based line number and the
error — never the pattern body (same rule as ``bin/memory/util.py``'s content
safety guard).

USAGE
-----
    git diff <range> -- ... | python3 bin/prepush/scan_diff.py \
        --patterns <file> --exclude-identities <regex> [--max-report N]

Exit 0 = nothing prohibited found. Exit 1 = hits found, or the policy could not
be evaluated. Exit 2 = called wrong.
"""

from __future__ import annotations

import argparse
import re
import sys


def load_patterns(path: str) -> list[tuple[int, re.Pattern[str]]]:
    """Compile every non-comment line, or raise with the line number."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read().splitlines()
    except OSError as e:
        raise SystemExit(
            f"[scan_diff] BLOCKED: cannot read the prohibited-pattern file.\n"
            f"           file  : {path}\n"
            f"           error : {e}\n"
            f"           A policy that cannot be read is not a policy that found "
            f"nothing. Fix the path (M3_PREPUSH_PROHIBITED) or restore the file."
        ) from e

    compiled: list[tuple[int, re.Pattern[str]]] = []
    broken: list[tuple[int, str]] = []
    for lineno, line in enumerate(raw, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            compiled.append((lineno, re.compile(line, re.IGNORECASE)))
        except re.error as e:
            broken.append((lineno, str(e)))

    if broken:
        detail = "\n".join(f"           {path}:{n} -> {err}  (pattern body withheld)" for n, err in broken)
        raise SystemExit(
            "[scan_diff] BLOCKED: prohibited-pattern file has pattern(s) that do "
            "not compile.\n"
            f"{detail}\n"
            "           The scan was NOT run. Fix the pattern(s) and push again."
        )
    if not compiled:
        raise SystemExit(
            f"[scan_diff] BLOCKED: no usable patterns in {path}.\n"
            "           An empty policy must not read as a clean scan."
        )
    return compiled


def scan_lines(
    lines: list[str],
    patterns: list[tuple[int, re.Pattern[str]]],
    placeholder: re.Pattern[str] | None,
    source: str,
) -> list[str]:
    """Return one report line per ADDED diff line that matches a pattern.

    Placeholder identities are blanked out of the line, not used to skip it:
    skipping let a real credential through whenever the same line also named a
    placeholder home path.
    """
    hits: list[str] = []
    for line in lines:
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        if placeholder is not None:
            body = placeholder.sub(" ", body)
        for lineno, pat in patterns:
            m = pat.search(body)
            if m:
                # Report WHERE and WHICH RULE, plus the offending diff line (the
                # author's own content, which they need in order to fix it) --
                # but never the pattern that matched it.
                where = f"{source}:{lineno}" if lineno else source
                hits.append(f"  rule {where} at col {m.start() + 1}: {line}")
                break
    return hits


def _selftest(patterns_path: str | None) -> int:
    """Prove the gate can still catch something, on this platform.

    A policy that will not compile is indistinguishable from a policy that found
    nothing unless something checks. This is that check, runnable by hand:

        python3 bin/prepush/scan_diff.py --selftest --patterns <policy>

    It loads the live policy through ``load_patterns`` (a count is reported,
    never the pattern text), then drives ``scan_lines`` -- the code the hook
    runs -- with a synthetic diff. Exit 0 only if every part holds.
    """
    import os
    import tempfile

    if not patterns_path:
        print(
            "[selftest] FAIL no --patterns given: there is no policy to prove alive.\n"
            "           Run: python bin/prepush/scan_diff.py --selftest --patterns "
            "<policy file>  (the hook reads M3_PREPUSH_PROHIBITED, else `git config "
            "m3.prepushProhibited`, else ~/.m3/prepush/prohibited.txt)",
            file=sys.stderr,
        )
        return 2

    ok = True
    try:
        pats = load_patterns(patterns_path)
    except SystemExit as e:
        print(f"[selftest] FAIL policy does not load:\n{e}")
        return 1
    print(f"[selftest] ok   {len(pats)} pattern(s) compiled from {patterns_path}")

    # A live pattern that matches an innocuous line blocks every push.
    innocuous = ["+++ b/x.py", "+nothing interesting here"]
    broad = scan_lines(innocuous, pats, None, patterns_path)
    if broad:
        print(f"[selftest] FAIL a live pattern matches an innocuous line: {broad[0].split(' at col')[0].strip()}")
        ok = False
    else:
        print("[selftest] ok   no live pattern matches an innocuous line")

    # The scanner itself, with its own probe so the answer does not depend on
    # what the live policy happens to contain.
    probe = [(1, re.compile("sk-" + "ant-" + r"[a-z0-9]{20,}", re.IGNORECASE))]
    key = "sk-" + "ant-" + "abcdefghij0123456789xyz"
    allow = re.compile(r"/home/(alice)([\\/]|$)", re.IGNORECASE)
    checks = [
        ("a credential-shaped added line is caught", [f'+KEY = "{key}"'], True),
        ("an innocuous added line is not caught", ["+nothing interesting here"], False),
        ("a removed line is not caught", [f'-KEY = "{key}"'], False),
        ("a +++ header is not caught", [f"+++ b/{key}.py"], False),
        ("a placeholder on the same line does not hide a credential", [f'+p = "/home/alice/x" KEY = "{key}"'], True),
    ]
    for label, lines, want in checks:
        got = bool(scan_lines(lines, probe, allow, "<selftest>"))
        print(f"[selftest] {'ok  ' if got == want else 'FAIL'} {label}")
        ok = ok and got == want

    # An uncompilable policy must be refused by load_patterns, not skipped.
    fd, bad = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("foo(\n")
        try:
            load_patterns(bad)
        except SystemExit:
            print("[selftest] ok   an uncompilable policy is refused by load_patterns")
        else:
            print("[selftest] FAIL an uncompilable policy was accepted by load_patterns")
            ok = False
    finally:
        os.unlink(bad)

    print(f"[selftest] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--patterns", help="policy file, one regex per line")
    ap.add_argument(
        "--inline-pattern",
        help="a single regex, used when no policy file is available (the hook's built-in credential/PII fallback)",
    )
    ap.add_argument("--exclude-identities", default="")
    ap.add_argument("--max-report", type=int, default=20)
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="prove the gate is alive on THIS platform: compile the "
        "live policy, and check a known-bad line is caught and a "
        "clean line is not. Reads no diff.",
    )
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest(args.patterns)

    if args.patterns:
        patterns = load_patterns(args.patterns)
        source = args.patterns
    elif args.inline_pattern:
        # The fallback is ONE joined regex by construction, so it is compiled as
        # one unit. It is short, built in and known to compile; the per-pattern
        # independence that matters is for the policy FILE, which is edited.
        try:
            patterns = [(0, re.compile(args.inline_pattern, re.IGNORECASE))]
        except re.error as e:
            raise SystemExit(f"[scan_diff] BLOCKED: the built-in fallback pattern does not compile: {e}") from e
        source = "<built-in fallback>"
    else:
        raise SystemExit(
            "[scan_diff] BLOCKED: no patterns given (need --patterns or "
            "--inline-pattern). Refusing to report a clean scan."
        )

    placeholder = None
    if args.exclude_identities:
        try:
            placeholder = re.compile(args.exclude_identities, re.IGNORECASE)
        except re.error as e:
            raise SystemExit(
                "[scan_diff] BLOCKED: the placeholder-identity allow-list regex "
                f"does not compile: {e}\n"
                "           Without it, legitimate doc placeholders would be "
                "reported as leaks, so the scan is not run."
            ) from e

    hits = scan_lines(sys.stdin.buffer.read().decode("utf-8", "replace").splitlines(), patterns, placeholder, source)

    if hits:
        print(
            f"[scan_diff] BLOCKED: prohibited content in the push diff ({len(hits)} added line(s) matched):",
            file=sys.stderr,
        )
        for h in hits[: args.max_report]:
            print(h, file=sys.stderr)
        if len(hits) > args.max_report:
            print(f"  ... and {len(hits) - args.max_report} more", file=sys.stderr)
        return 1

    print(f"[scan_diff] {len(patterns)} pattern(s) evaluated; no prohibited content.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
