"""Comments and docstrings say what the code does and why, not how it got here.

Change history belongs in git and the CHANGELOG. A comment block or docstring
that narrates it (a calendar date, a PR or issue number, "used to",
"previously", "we changed") is counted; the count may only go down, and must be
lowered when it does. A site that needs its history, such as a measurement whose
date is the evidence, says so with the token ``history-ok`` anywhere in that
comment block or docstring.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

from conftest import BUILD_STAGED_COPIES

_ROOT = Path(__file__).resolve().parents[1]
_MARKER = "history-ok"

PATTERNS = {
    "date": re.compile(r"\b20\d\d-\d\d-\d\d\b"),
    "pr": re.compile(r"\b(?:PR|[Pp]ull request|[Ii]ssue|GH)[ -]?#?\d{2,}\b|(?<![\w&/#])#\d{2,4}\b(?![0-9a-fA-F])"),
    "narration": re.compile(
        # "is used to" states a purpose; the lookbehinds leave it out.
        r"(?<!\bis )(?<!\bare )(?<!\bbe )(?<!\bbeen )(?<!\bbeing )\bused to\b"
        r"|\b[Pp]reviously\b|\b[Ff]ormerly\b"
        r"|\bwas (?:changed|renamed|removed|added|moved|fixed|replaced)\b"
        r"|\b[Ww]e (?:changed|removed|added|moved|fixed|switched|replaced)\b"
        r"|\b[Tt]his (?:PR|commit)\b"
    ),
}

# Current floor. Lower this as history leaves the comments; never raise it.
_BUDGET = 1097


def _texts(src: str) -> list[tuple[int, str]]:
    """Each comment block (consecutive whole-line comments; a trailing comment
    stands alone) and docstring, with its first line."""
    out: list[tuple[int, str]] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
        tree = ast.parse(src)
    except (tokenize.TokenError, SyntaxError):
        return out
    block: list[str] = []
    first = last = 0
    joinable = False
    for t in toks:
        if t.type != tokenize.COMMENT:
            continue
        line = t.start[0]
        whole = t.line.lstrip().startswith("#")
        if block and joinable and whole and line == last + 1:
            block.append(t.string)
        else:
            if block:
                out.append((first, "\n".join(block)))
            block, first = [t.string], line
        last, joinable = line, whole
    if block:
        out.append((first, "\n".join(block)))
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(n, clean=False)
            if doc:
                out.append((n.body[0].lineno, doc))
    return out


def _sites(roots=("bin", "m3_memory", "tests"), base: Path = _ROOT) -> list[str]:
    found = []
    for root in roots:
        for p in sorted((base / root).rglob("*.py")):
            rel = p.relative_to(base).as_posix()
            if rel in BUILD_STAGED_COPIES:
                continue
            for line, text in _texts(p.read_text(encoding="utf-8")):
                if _MARKER in text:
                    continue
                kinds = [k for k, pat in PATTERNS.items() if pat.search(text)]
                if kinds:
                    found.append(f"{rel}:{line} ({', '.join(kinds)})")
    return found


def test_history_in_comments_does_not_grow():
    sites = _sites()
    assert len(sites) <= _BUDGET, (
        f"observed: comment blocks/docstrings narrating history rose from {_BUDGET} to {len(sites)}. "
        "inspect: say what the code does and why; put the history in the commit message or "
        f"CHANGELOG, or mark a site whose history is the evidence with `{_MARKER}`"
    )


def test_the_budget_is_not_stale():
    sites = _sites()
    assert len(sites) == _BUDGET, (
        f"observed: history in comments is down to {len(sites)}. "
        "inspect: lower _BUDGET in tests/test_comment_history_ratchet.py to lock it in"
    )


def test_the_gate_sees_each_form_and_honours_the_marker(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "m.py").write_text(
        '"""Measured 2030-01-02."""\n'
        "x = 1  # see issue #12\n"
        "# this used to retry\n"
        "\n"
        "# it is used to gate writes\n"
        "\n"
        "# 2030-01-02 measured; history-ok\n"
        "# continues the marked block\n"
        "def f():\n"
        '    """Formerly the writer."""\n',
        encoding="utf-8",
    )
    assert _sites(("bin",), tmp_path) == [
        "bin/m.py:2 (pr)",
        "bin/m.py:3 (narration)",
        "bin/m.py:1 (date)",
        "bin/m.py:10 (narration)",
    ]
