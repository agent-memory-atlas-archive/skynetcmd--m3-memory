"""Tests reach `memory.*` modules with importlib.import_module, not `import memory.X as Y`.

Inside a test body, `import memory.X as Y` binds the PARENT package's attribute.
After conftest purges the `memory.*` namespace that attribute can be a stale
module: `importlib.reload` then raised "module memory.embed not in sys.modules",
and a monkeypatch lands on an object the code under test no longer reads
(.claude/rules/test-sandbox.md). `importlib.import_module` reads sys.modules.
Module-level imports run at collection, before any purge, and are not checked.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent

# (file, function) pairs that use the plain statement on purpose.
_ALLOWED = {("test_backends_dialect_not_rebound.py", "test_dialect_is_the_accessor_after_a_normal_import")}


def test_no_function_level_import_memory_as():
    offenders = []
    for path in sorted(_TESTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (path.name, fn.name) in _ALLOWED:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.startswith("memory.") and a.asname:
                            offenders.append(f"{path.name}:{node.lineno} import {a.name} as {a.asname}")
    assert not offenders, 'use `X = importlib.import_module("memory.x")` inside a test body:\n  ' + "\n  ".join(
        offenders
    )
