"""No module resolves the storage backend while it is being imported.

The backend is chosen by `[db] backend`, by `--set`, or by a test's
`use_backend()` — all of which run AFTER imports. A module that resolves it at
import time binds whatever was selected first: `bin/pg_sync.py` had
`_LOCAL_PARAM = _local_param()`, which built the active backend (a PostgreSQL
connection) while pytest was still collecting, and five test files failed to
collect on the PostgreSQL lane once the lanes stopped exporting the DSN
(2026-10-12). The call was indirect — `_local_param` -> `_local_dialect` ->
`seam_dialect` — so this follows calls to functions defined in the same module.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Each of these reads the selection (and may build the backend).
_RESOLVERS = frozenset(
    {
        "seam_backend",
        "seam_dialect",
        "active_backend",
        "resolve_backend_name",
        "require_sqlite_backend",
        "store_is_a_file",
    }
)
# `dialect()` is too common a name to match bare; match it on the selector.
_RESOLVER_ATTRS = {("selector", "dialect"), ("backends", "dialect")}


def _called_name(call: ast.Call) -> tuple[str | None, str | None]:
    """(base, name) of a call: `f()` -> (None, "f"); `a.f()` -> ("a", "f")."""
    f = call.func
    if isinstance(f, ast.Name):
        return None, f.id
    if isinstance(f, ast.Attribute):
        base = f.value.id if isinstance(f.value, ast.Name) else None
        return base, f.attr
    return None, None


def _is_resolver(call: ast.Call) -> bool:
    base, name = _called_name(call)
    return name in _RESOLVERS or (base, name) in _RESOLVER_ATTRS


def _is_main_guard(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)


def _import_time_nodes(body: list[ast.stmt]):
    """Every node that runs when the module is imported: top-level and class-body
    statements, plus decorators and default values of functions (evaluated at
    `def`). Function and lambda BODIES do not run; neither does a __main__ block."""
    for stmt in body:
        if _is_main_guard(stmt):
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from stmt.decorator_list
            yield from stmt.args.defaults
            yield from (d for d in stmt.args.kw_defaults if d is not None)
            continue
        if isinstance(stmt, ast.ClassDef):
            yield from stmt.decorator_list
            yield from stmt.bases
            yield from _import_time_nodes(stmt.body)
            continue
        yield stmt


def _calls(nodes) -> list[ast.Call]:
    out = []
    stack = list(nodes)
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Call):
            out.append(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue  # defined here, not run here
        stack.extend(ast.iter_child_nodes(n))
    return out


def _resolves(tree: ast.Module) -> list[str]:
    """Import-time call chains in this module that reach a resolver."""
    defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def chain(call: ast.Call, seen: tuple[str, ...]) -> tuple[str, ...] | None:
        if _is_resolver(call):
            return (*seen, _called_name(call)[1] or "?")
        base, name = _called_name(call)
        if base is None and name in defs and name not in seen:
            for inner in _calls(defs[name].body):
                found = chain(inner, (*seen, name))
                if found:
                    return found
        return None

    hits = []
    for call in _calls(_import_time_nodes(tree.body)):
        found = chain(call, ())
        if found:
            hits.append(f"line {call.lineno}: {' -> '.join(found)}")
    return hits


def test_no_module_resolves_the_backend_at_import():
    problems = []
    files = sorted((*(REPO / "bin").rglob("*.py"), *(REPO / "m3_memory").rglob("*.py")))
    assert len(files) > 100, f"scanned only {len(files)} files; the glob is wrong"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        problems += [f"{path.relative_to(REPO).as_posix()} {hit}" for hit in _resolves(tree)]
    assert not problems, (
        "resolved the backend while importing; resolve it at call time "
        "(the selection is made after imports):\n  " + "\n  ".join(problems)
    )


def test_the_scan_follows_a_same_module_call():
    """The shape that shipped: a module-level constant from a local helper that
    reaches the seam. A direct-only scan misses it."""
    src = (
        "def _d():\n    return seam_dialect()\n"
        "def _p():\n    return _d().param()\n"
        "P = _p()\n"
        "def later():\n    return seam_dialect()\n"
        "if __name__ == '__main__':\n    seam_backend()\n"
    )
    assert _resolves(ast.parse(src)) == ["line 5: _p -> _d -> seam_dialect"]
