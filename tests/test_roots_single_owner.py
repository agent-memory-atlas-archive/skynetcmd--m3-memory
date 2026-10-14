"""The root variables are read in ONE place: m3_memory/_roots.py.

The config/engine/logs/models/memory/lib root rule was copied into ~20 files: the copies drifted (one ignored
M3_LOGS_ROOT, one never expanded `~`, two defaulted the memory root
differently), so a reader outside the owner is the defect, not a style issue.
Writes are not reads: the two env WRITES below set roots for a child process and
move to Phase 2E's `child_env`.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_NAMES = {"M3_MEMORY_ROOT", "M3_CONFIG_ROOT", "M3_ENGINE_ROOT", "M3_LOGS_ROOT", "M3_MODELS_ROOT", "M3_LIB_DIR"}
_OWNER = "m3_memory/_roots.py"


def _is_environ(node: ast.AST) -> bool:
    """`os.environ` / `_os.environ` / a bare `environ` (from os import environ)."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _reads(tree: ast.AST):
    """(lineno, name) for os.environ.get(N) / os.getenv(N) / os.environ[N] loads."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            # Only the process environment: `cfg.get("M3_ENGINE_ROOT")` on a dict read
            # from a client's config file is not an env read.
            on_environ = name == "getenv" or (isinstance(f, ast.Attribute) and _is_environ(f.value))
            if on_environ and name in ("get", "getenv") and n.args[0].value in _NAMES:
                yield n.lineno, n.args[0].value
        elif (
            isinstance(n, ast.Subscript)
            and isinstance(n.ctx, ast.Load)
            and _is_environ(n.value)
            and isinstance(n.slice, ast.Constant)
            and n.slice.value in _NAMES
        ):
            yield n.lineno, n.slice.value


def test_root_variables_are_read_only_by_their_owner():
    hits = []
    for base in ("bin", "m3_memory"):
        for p in sorted((_ROOT / base).rglob("*.py")):
            rel = p.relative_to(_ROOT).as_posix()
            if "__pycache__" in p.parts or rel == _OWNER:
                continue
            hits += [f"{rel}:{ln} reads {name}" for ln, name in _reads(ast.parse(p.read_text(encoding="utf-8")))]
    assert not hits, "read the root through m3_memory._roots (bin/: m3_core.seams.roots):\n  " + "\n  ".join(hits)


def test_the_probe_sees_each_read_shape():
    """The guard must actually catch the three spellings, or it passes vacuously."""
    # The last line is a dict read from a client config, not the environment: it must NOT count.
    src = 'import os\nos.environ.get("M3_ENGINE_ROOT")\nos.getenv("M3_LOGS_ROOT")\nx = os.environ["M3_LIB_DIR"]\ncfg.get("M3_CONFIG_ROOT")\n'
    assert sorted(name for _, name in _reads(ast.parse(src))) == ["M3_ENGINE_ROOT", "M3_LIB_DIR", "M3_LOGS_ROOT"]
