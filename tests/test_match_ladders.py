"""An if/elif ladder that only compares ONE subject with literals, 4+ times,
is written as `match` (3.10+), unless listed here with a reason.

ruff has no rule for this, so this static scan is the gate.
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MIN_ARMS = 4

# (file, function) -> why the ladder stays.
_KEEP = {
    ("bin/dashboard_server.py", "make_html_diff"):
        "arm bodies hold multi-line string literals; re-indenting them changes the HTML",
    ("bin/dashboard_server.py", "render_audit_card"):
        "arm bodies hold multi-line string literals; re-indenting them changes the HTML",
}


def _literal_arm(test: ast.expr) -> str | None:
    """The subject of `x == <literal>` or `x in (<literals>)`, else None."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return None
    op, right = test.ops[0], test.comparators[0]
    if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
        return ast.unparse(test.left)
    if (isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set))
            and all(isinstance(e, ast.Constant) for e in right.elts)):
        return ast.unparse(test.left)
    return None


def _owners(tree: ast.AST) -> dict[int, str]:
    """id(If) -> the innermost function enclosing it ('<module>' at top level)."""
    owner: dict[int, str] = {}

    def visit(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.If):
                owner[id(child)] = fn
            visit(child, name)

    visit(tree, "<module>")
    return owner


def _ladders(roots=("bin", "m3_memory"), base: Path = _ROOT) -> set[tuple[str, str]]:
    found = set()
    for root in roots:
        for p in (base / root).rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            owner = _owners(tree)
            inner: set[int] = set()
            for n in ast.walk(tree):
                if not isinstance(n, ast.If) or id(n) in inner:
                    continue
                subjects, cur = [], n
                while True:
                    subjects.append(_literal_arm(cur.test))
                    if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
                        cur = cur.orelse[0]
                        inner.add(id(cur))
                    else:
                        break
                if (len(subjects) >= _MIN_ARMS and None not in subjects
                        and len(set(subjects)) == 1):
                    found.add((p.relative_to(base).as_posix(), owner[id(n)]))
    return found


def test_literal_ladders_are_match_statements():
    extra = _ladders() - set(_KEEP)
    assert not extra, (
        "if/elif ladder over one subject and literals; write it as `match` "
        "(or list it in _KEEP with the reason):\n  "
        + "\n  ".join(f"{f}: {fn}()" for f, fn in sorted(extra)))


def test_the_keep_list_is_not_stale():
    stale = set(_KEEP) - _ladders()
    assert not stale, f"no longer a ladder, drop from _KEEP: {sorted(stale)}"


def test_the_scan_recognises_a_ladder(tmp_path):
    src = tmp_path / "bin"
    src.mkdir()
    (src / "m.py").write_text(
        "def f(x):\n"
        "    if x == 'a':\n        return 1\n"
        "    elif x in ('b', 'c'):\n        return 2\n"
        "    elif x == 'd':\n        return 3\n"
        "    elif x == 'e':\n        return 4\n"
        "def g(x, y):\n"  # mixed subjects: not a ladder
        "    if x == 1:\n        pass\n    elif y == 2:\n        pass\n"
        "    elif x == 3:\n        pass\n    elif x == 4:\n        pass\n"
    )
    assert _ladders(("bin",), tmp_path) == {("bin/m.py", "f")}
