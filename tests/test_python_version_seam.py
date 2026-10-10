"""Python-version differences live in one module, m3_memory/_pyver.py.

The interpreter-version counterpart of the database dialect: call sites ask the
seam what they need (a wheel tag, the asyncio timer slack) instead of testing
sys.version_info themselves, so raising the floor means reading one module.
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SEAM = _ROOT / "m3_memory" / "_pyver.py"
_VERSION_ATTRS = {"version_info", "hexversion"}


def _offenders(roots, base: Path = _ROOT) -> list[str]:
    out = []
    for root in roots:
        for p in sorted((base / root).rglob("*.py")):
            if p == _SEAM:
                continue
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if (isinstance(n, ast.Attribute) and n.attr in _VERSION_ATTRS
                        and isinstance(n.value, ast.Name) and n.value.id == "sys"):
                    out.append(f"{p.relative_to(base).as_posix()}:{n.lineno}")
    return out


def test_version_checks_live_in_the_seam():
    found = _offenders(("bin", "m3_memory", "tests"))
    assert not found, (
        "sys.version_info outside m3_memory/_pyver.py; add what you need to the "
        "version seam and call it:\n  " + "\n  ".join(found))


def test_the_gate_sees_a_version_check(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "m.py").write_text(
        "import sys\nif sys.version_info >= (3, 13):\n    pass\nx = sys.version\n", encoding="utf-8")
    assert _offenders(("bin",), tmp_path) == ["bin/m.py:2"]
