"""Every dataclass in shipped code is slotted, unless listed here with a reason.

slots=True (3.10+) gives smaller instances, faster attribute access, and turns a
misspelled attribute assignment into an AttributeError instead of a silently
added attribute. ruff has no rule for it, so this static scan is the gate.
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# (file, class) -> why it cannot be slotted.
_UNSLOTTED = {
    ("bin/wiki/citation_drift.py", "DriftConfig"):
        "from_env reads field defaults off the class (cls.url); with slots that is a member descriptor",
    ("bin/wiki/prose_compiler.py", "ProseCompilerConfig"):
        "from_env reads field defaults off the class (cls.url); with slots that is a member descriptor",
    ("bin/wiki/synth.py", "SynthConfig"):
        "from_env reads field defaults off the class (cls.url); with slots that is a member descriptor",
}


def _dataclass_decorator(c: ast.ClassDef) -> ast.expr | None:
    for d in c.decorator_list:
        f = d.func if isinstance(d, ast.Call) else d
        if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) == "dataclass":
            return d
    return None


def _unslotted(roots=("bin", "m3_memory"), base: Path = _ROOT) -> set[tuple[str, str]]:
    found = set()
    for root in roots:
        for p in (base / root).rglob("*.py"):
            for c in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if not isinstance(c, ast.ClassDef):
                    continue
                d = _dataclass_decorator(c)
                if d is None:
                    continue
                slotted = isinstance(d, ast.Call) and any(
                    k.arg == "slots" and isinstance(k.value, ast.Constant) and k.value.value
                    for k in d.keywords)
                if not slotted:
                    found.add((p.relative_to(base).as_posix(), c.name))
    return found


def test_dataclasses_are_slotted():
    extra = _unslotted() - set(_UNSLOTTED)
    assert not extra, (
        "dataclass without slots=True (add it, or list it in _UNSLOTTED with the reason):\n  "
        + "\n  ".join(f"{f}: {c}" for f, c in sorted(extra)))


def test_the_allowlist_is_not_stale():
    stale = set(_UNSLOTTED) - _unslotted()
    assert not stale, f"no longer unslotted, drop from _UNSLOTTED: {sorted(stale)}"


def test_the_scan_sees_each_decorator_form(tmp_path):
    src = tmp_path / "bin"
    src.mkdir()
    (src / "m.py").write_text(
        "import dataclasses\n"
        "from dataclasses import dataclass\n"
        "@dataclass\nclass A:\n    x: int = 0\n"
        "@dataclass(frozen=True)\nclass B:\n    x: int = 0\n"
        "@dataclasses.dataclass\nclass C:\n    x: int = 0\n"
        "@dataclass(slots=True)\nclass D:\n    x: int = 0\n"
    )
    assert _unslotted(("bin",), tmp_path) == {("bin/m.py", "A"), ("bin/m.py", "B"), ("bin/m.py", "C")}
