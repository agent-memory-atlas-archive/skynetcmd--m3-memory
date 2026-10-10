"""OS checks move into the OS seam, m3_memory/_platform.py, and never grow outside it.

The seam hierarchy (version < OS < database < business logic; see
m3_memory/_pyver.py) says call sites ask the OS seam (`is_windows()`,
`os_name()`, `is_apple_silicon()`, `hidden_window_kwargs()`) instead of testing
os.name / sys.platform / platform.* themselves. 187 such checks predate the rule,
so this is a ratchet like the error-idiom budget: the count may only go down, and
must be lowered when it does. `platform.system/machine/release` are banned
outright outside the seam: on Windows + Python 3.14 they can hang on a WMI query.
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SEAM = _ROOT / "m3_memory" / "_platform.py"

# Current floor. Lower this as sites move into the seam; never raise it.
# 2026-10-10: 187 -> 180 (mission control and the Apple-Silicon checks moved).
# 2026-10-10: 180 -> 178 (install_schedules asks the seam).
_BUDGET = 178


def _sites(roots=("bin", "m3_memory"), base: Path = _ROOT) -> tuple[list[str], list[str]]:
    checks, banned = [], []
    for root in roots:
        for p in sorted((base / root).rglob("*.py")):
            if p == _SEAM:
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            rel = p.relative_to(base).as_posix()
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)):
                    continue
                pair = (n.value.id, n.attr)
                if pair in (("os", "name"), ("sys", "platform")):
                    checks.append(f"{rel}:{n.lineno}")
                elif n.value.id == "platform" and n.attr in ("system", "machine", "release", "uname",
                                                             "win32_ver", "mac_ver"):
                    banned.append(f"{rel}:{n.lineno} platform.{n.attr}")
    return checks, banned


def test_no_wmi_prone_platform_calls_outside_the_seam():
    _, banned = _sites()
    assert not banned, ("platform.* outside m3_memory/_platform.py (WMI hang on Windows + 3.14); "
                        "use os_name()/is_windows()/is_apple_silicon():\n  " + "\n  ".join(banned))


def test_os_checks_outside_the_seam_do_not_grow():
    checks, _ = _sites()
    assert len(checks) <= _BUDGET, (
        f"OS checks outside the seam rose from {_BUDGET} to {len(checks)}. Ask the OS seam "
        "(m3_memory/_platform.py) instead, adding the fact there if it is missing.")


def test_the_budget_is_not_stale():
    checks, _ = _sites()
    assert len(checks) == _BUDGET, (
        f"OS checks outside the seam are down to {len(checks)}; lower _BUDGET to lock it in")


def test_the_gate_sees_each_form(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "m.py").write_text(
        "import os, sys, platform\n"
        "a = os.name == 'nt'\nb = sys.platform == 'darwin'\nc = platform.system()\n",
        encoding="utf-8")
    checks, banned = _sites(("bin",), tmp_path)
    assert checks == ["bin/m.py:2", "bin/m.py:3"] and banned == ["bin/m.py:4 platform.system"]
