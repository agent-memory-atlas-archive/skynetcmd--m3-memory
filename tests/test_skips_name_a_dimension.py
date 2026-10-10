"""Every skip names the dimension that caused it (conftest.SKIP_DIMENSIONS).

A run then reports what did not run and why (os / python / db / dep / env / na),
and an `na` skip, the kind that can hide a missing assertion, is listed for
review. A marker-driven skip (requires_*) is tagged by conftest; an in-test
skip must build its reason with conftest.skip_reason.
"""
from __future__ import annotations

import ast
import pathlib

_TESTS = pathlib.Path(__file__).resolve().parent
_SKIPS = {"pytest.skip", "pytest.importorskip", "pytest.mark.skipif", "pytest.mark.skip"}


def _untagged(tree: ast.AST) -> list[int]:
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and ast.unparse(n.func) in _SKIPS):
            continue
        reason = next((kw.value for kw in n.keywords if kw.arg in ("reason", "msg")), None)
        if reason is None and ast.unparse(n.func) in ("pytest.skip", "pytest.mark.skip") and n.args:
            reason = n.args[0]
        if not (isinstance(reason, ast.Call) and ast.unparse(reason.func) == "skip_reason"):
            out.append(n.lineno)
    return out


def test_every_skip_names_a_dimension():
    hits = [f"{p.name}:{ln}" for p in sorted(_TESTS.glob("test_*.py"))
            for ln in _untagged(ast.parse(p.read_text(encoding="utf-8")))]
    assert not hits, ("observed: a skip whose reason names no dimension: " + ", ".join(hits)
                      + ". inspect: reason=skip_reason(<os|python|db|dep|env|na>, ...) from "
                      "conftest, or a requires_* marker")


def test_skip_reason_rejects_an_unknown_dimension():
    import pytest

    from conftest import skip_reason

    assert skip_reason("os", "x") == "[os] x"
    with pytest.raises(ValueError):
        skip_reason("misc", "x")
