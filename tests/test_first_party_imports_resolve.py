"""Every `from <first-party module> import <name>` names something that exists.

Renaming `files_memory.db._is_postgres` left four callers importing the old
name. Each import sat inside `except Exception`, so the ImportError vanished
and on PostgreSQL the status tools described the files store as a SQLite file
(2026-10-10). Static: reads the AST of both sides, imports nothing.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREES = (ROOT / "bin", ROOT / "m3_memory")

# Known stale imports, each with its reason and owner. An entry that stops
# matching fails the test, so this list can only shrink.
KNOWN: dict[str, str] = {}


def _module_file(dotted: str) -> Path | None:
    parts = dotted.split(".")
    for base in (ROOT / "bin", ROOT):
        p = base.joinpath(*parts)
        for cand in (p.with_suffix(".py"), p / "__init__.py"):
            if cand.is_file():
                return cand
    return None


def _defined(path: Path) -> set[str] | None:
    """Names bound at module top level, or None when they cannot be known
    statically (a star import or a module-level __getattr__)."""
    text = path.read_text(encoding="utf-8")
    if "globals()[" in text:
        return None  # names bound at runtime (memory_bridge registers its tools so)
    tree = ast.parse(text)
    names: set[str] = set()

    def bind(node: ast.AST) -> None:
        for t in ast.walk(node):
            if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                names.add(t.id)

    def visit(body: list[ast.stmt]) -> bool:
        for n in body:
            if isinstance(n, ast.TypeAlias):  # PEP 695 `type X = ...`
                names.add(n.name.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
                if n.name == "__getattr__":
                    return False
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    if a.name == "*":
                        return False
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.For, ast.With)):
                bind(n)
                if isinstance(n, (ast.For, ast.With)) and not visit(n.body):
                    return False
            elif isinstance(n, (ast.If, ast.Try)):
                blocks = [n.body, n.orelse] + ([h.body for h in n.handlers] + [n.finalbody]
                                               if isinstance(n, ast.Try) else [])
                if not all(visit(b) for b in blocks):
                    return False
        return True

    return names if visit(tree.body) else None


def _package_of(path: Path) -> str:
    rel = path.relative_to(ROOT / "bin") if path.is_relative_to(ROOT / "bin") else path.relative_to(ROOT)
    return ".".join(rel.with_suffix("").parts[:-1])


def test_imported_names_exist():
    cache: dict[Path, set[str] | None] = {}
    missing: list[tuple[str, int]] = []
    for tree_root in TREES:
        for src in sorted(tree_root.rglob("*.py")):
            if "__pycache__" in src.parts:
                continue
            for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                dotted = node.module
                if node.level:
                    pkg = _package_of(src).split(".") if _package_of(src) else []
                    pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                    dotted = ".".join([*pkg, node.module])
                target = _module_file(dotted)
                if target is None:
                    continue  # third-party or stdlib
                if target not in cache:
                    cache[target] = _defined(target)
                defined = cache[target]
                if defined is None:
                    continue
                for a in node.names:
                    if a.name in defined or _module_file(f"{dotted}.{a.name}"):
                        continue
                    key = f"{src.relative_to(ROOT).as_posix()} imports {a.name!r} from {dotted}"
                    missing.append((key, node.lineno))
    stale_known = set(KNOWN) - {k for k, _ in missing}
    assert not stale_known, f"observed: KNOWN entries no longer seen: {stale_known}. inspect: delete them"
    missing = [f"{k} (line {ln})" for k, ln in missing if k not in KNOWN]
    assert not missing, ("observed: imports of names their module does not define:\n  "
                         + "\n  ".join(missing)
                         + "\ninspect: a rename left these callers behind; a broad except around one hides it")
