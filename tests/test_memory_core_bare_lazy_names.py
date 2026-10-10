"""memory_core's own functions use a lazily exported name only if it is bound.

memory_core exports ~230 names through a module __getattr__, which Python does
NOT consult for a bare-name lookup inside the module. The eager-bind block at
the bottom of memory_core resolves the eight symbols its functions call by
bare name; config SETTINGS are deliberately never bound (read at call time,
2D.1). conversation_summarize used the setting LLM_TIMEOUT by bare name and
raised NameError (found by bin/test_memory_bridge.py, 2026-10-10).

Static: every bare-name read of a lazy export inside a memory_core function
must be one of the eagerly bound symbols.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "bin" / "memory_core.py"


def _eager_and_lazy(tree: ast.Module) -> tuple[set[str], set[str]]:
    lazy: set[str] = set()
    eager: set[str] = set()
    for n in tree.body:
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
        if any(getattr(t, "id", "") == "_LAZY_IMPORTS" for t in targets):
            lazy = set(ast.literal_eval(n.value).keys())
        if isinstance(n, ast.For) and getattr(n.target, "id", "") == "_eager":
            eager = {e.value for e in n.iter.elts}
    assert lazy and eager, "observed: _LAZY_IMPORTS or the _eager block not found. inspect: bin/memory_core.py"
    return lazy, eager


def test_bare_lazy_reads_are_eagerly_bound():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    lazy, eager = _eager_and_lazy(tree)
    module_bound = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            module_bound |= {(a.asname or a.name).split(".")[0] for a in n.names}
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name == "__getattr__":
            continue
        local = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        for x in ast.walk(fn):
            if isinstance(x, (ast.Import, ast.ImportFrom)):
                local |= {(a.asname or a.name).split(".")[0] for a in x.names}
            elif isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                local.add(x.id)
        for x in ast.walk(fn):
            if (isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in lazy
                    and x.id not in local | module_bound | eager):
                bad.append(f"{fn.name}:{x.lineno} {x.id}")
    assert not bad, ("observed: bare reads of lazy exports that nothing binds (NameError at call time): "
                     + ", ".join(sorted(set(bad)))
                     + ". inspect: a setting is read from memory.config at call time; a symbol "
                       "goes in memory_core's _eager block")
