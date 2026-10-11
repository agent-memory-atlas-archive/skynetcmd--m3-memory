"""No function that takes a parameter named ``type`` calls ``type(...)``.

memory.write's auto-link failure handler formatted ``type(le).__name__`` inside
a function whose ``type`` parameter is the memory type (a str), so the warning
itself raised ``TypeError: 'str' object is not callable`` whenever auto-linking
failed. mypy did not report it where it first lived; this scan does.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _hits(source: str, rel: str) -> list[str]:
    out = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = fn.args
        if "type" not in {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}:
            continue
        out += [
            f"{rel}:{n.lineno} in {fn.name}()"
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "type"
        ]
    return out


def test_no_type_call_under_a_type_parameter():
    hits = []
    for root in ("bin", "m3_memory"):
        for p in sorted((_ROOT / root).rglob("*.py")):
            rel = p.relative_to(_ROOT).as_posix()
            hits += _hits(p.read_text(encoding="utf-8"), rel)
    assert not hits, (
        "type(...) called where a `type` parameter shadows the builtin; use x.__class__:\n  " + "\n  ".join(hits)
    )


def test_the_scan_can_fail():
    planted = "def f(type):\n    try:\n        pass\n    except Exception as e:\n        return type(e).__name__\n"
    assert _hits(planted, "planted.py") == ["planted.py:5 in f()"]
