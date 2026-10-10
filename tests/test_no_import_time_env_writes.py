"""No module under tests/ writes os.environ at import time.

The manual scripts here (capture_*, integration_*, smoke_*, bench_*) once set
M3_EMBED_GGUF and friends at module level, so merely importing one (pytest given
it by name) leaked a placeholder model path into every later test. A script sets
its environment under `if __name__ == "__main__":`; a test that needs a value
uses monkeypatch, or saves and restores it (test_redaction_parity does).
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
# Module-level writes that are deliberately restored in a finally block.
_RESTORED = {"test_redaction_parity.py": "saves M3_CORE_RS_DISABLE and restores it in finally"}


def _module_level_env_writes(tree: ast.Module) -> list[int]:
    out = []
    for n in tree.body:
        s = ast.unparse(n)
        if isinstance(n, ast.Expr) and "os.environ.setdefault(" in s:
            out.append(n.lineno)
        elif isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Subscript) and ast.unparse(t.value) == "os.environ" for t in n.targets
        ):
            out.append(n.lineno)
    return out


def test_no_module_writes_the_environment_at_import():
    hits = []
    for p in sorted(_TESTS.glob("*.py")):
        if p.name == "conftest.py" or p.name in _RESTORED:
            continue
        hits += [f"{p.name}:{ln}" for ln in _module_level_env_writes(ast.parse(p.read_text(encoding="utf-8")))]
    assert not hits, (
        "observed: os.environ written at import: "
        + ", ".join(hits)
        + '. inspect: move it under `if __name__ == "__main__":`, or use monkeypatch'
    )
