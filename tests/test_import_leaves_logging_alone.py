"""A module other m3 code imports must not configure logging at import.

Found in a trial upgrade: every `m3 memory ...` command printed
`httpx: [INFO] HTTP Request: POST .../embedding` and internal search notes,
because the CLI imports migrate_memory, which called logging.basicConfig(INFO)
at import and so switched INFO output on for every library in the process.
A script configures logging in main(); an importer keeps its own settings.

Static, so it runs the same on every OS and executes no module body.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_BIN = os.path.join(_ROOT, "bin")
_SCANNED = ("bin", "m3_memory", "memory")


def _module_level_basicconfig(path: str) -> bool:
    """True when basicConfig runs at import: a top-level statement, or one
    nested only in top-level if/try blocks (not in a def or class)."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))

    def walk(stmts) -> bool:
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.If) and _is_main_guard(node):
                continue
            for sub in ast.walk(node) if isinstance(node, ast.Expr) else ():
                if isinstance(sub, ast.Call) and getattr(sub.func, "attr", "") == "basicConfig":
                    return True
            for field in ("body", "orelse", "finalbody", "handlers"):
                if isinstance(getattr(node, field, None), list) and walk(getattr(node, field)):
                    return True
        return False

    return walk(tree.body)


def _is_main_guard(node: ast.If) -> bool:
    t = node.test
    return (
        isinstance(t, ast.Compare)
        and isinstance(t.left, ast.Name)
        and t.left.id == "__name__"
        and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in t.comparators)
    )


def _importers(module: str) -> list[str]:
    pat = re.compile(rf"^\s*(?:import|from)\s+{re.escape(module)}\b|import_module\(\s*[\"']{re.escape(module)}[\"']", re.M)
    found = []
    for top in _SCANNED:
        for dirpath, _dirs, files in os.walk(os.path.join(_ROOT, top)):
            for f in files:
                if not f.endswith(".py") or f.startswith("test_"):
                    continue
                p = os.path.join(dirpath, f)
                if os.path.normpath(p) == os.path.normpath(os.path.join(_BIN, module + ".py")):
                    continue
                if pat.search(Path(p).read_text(encoding="utf-8", errors="replace")):
                    found.append(os.path.relpath(p, _ROOT))
    return found


def test_imported_modules_do_not_configure_logging_at_import():
    offenders = {}
    for f in sorted(os.listdir(_BIN)):
        if not f.endswith(".py"):
            continue
        if _module_level_basicconfig(os.path.join(_BIN, f)):
            importers = _importers(f[:-3])
            if importers:
                offenders[f] = importers
    assert not offenders, (
        "these modules call logging.basicConfig at import yet other m3 code imports "
        f"them; move the call into main(): {offenders}"
    )


def test_the_check_sees_a_module_level_call(tmp_path):
    """The guard must be able to fail."""
    bad = tmp_path / "bad.py"
    bad.write_text("import logging\nlogging.basicConfig(level=logging.INFO)\n", encoding="utf-8")
    good = tmp_path / "good.py"
    good.write_text(
        "import logging\ndef main():\n    logging.basicConfig()\nif __name__ == '__main__':\n"
        "    logging.basicConfig()\n",
        encoding="utf-8",
    )
    assert _module_level_basicconfig(str(bad))
    assert not _module_level_basicconfig(str(good))
