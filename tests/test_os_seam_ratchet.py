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

# Code that must run while the m3_memory package is ABSENT or being replaced,
# so it cannot import the seam: it keeps its own stdlib OS check. Each entry
# carries its reason; the ratchet below counts everything else.
EXEMPT = {
    "bin/m3_embed_watchdog.py": "restarts m3 when the package is broken (parity: test_os_seam_watchdog_parity)",
    "bin/m3_loop_watchdog.py": "restarts m3 when the package is broken (parity: test_os_seam_watchdog_parity)",
    "bin/setup_memory.py": "bootstrap: creates the first venv/store before anything is installed",
    "bin/generate_configs.py": "bootstrap pair with setup_memory.py (tests/test_setup_memory_interpreter.py)",
    "bin/test_memory_bridge.py": "documented standalone `python3 bin/test_memory_bridge.py` (AGENT_INSTRUCTIONS)",
}

# Current floor. Lower this as sites move into the seam; never raise it.
# 2026-10-10: 187 -> 180 (mission control and the Apple-Silicon checks moved).
# 2026-10-10: 180 -> 178 (install_schedules asks the seam).
# 2026-10-10: 178 -> 5 (the AST migration; EXEMPT files are not counted; the 5
# left only print the platform name).
# 2026-10-12: 5 -> 4 (store_pg_dsn no longer picks a store per OS, so its
# "not used on <platform>" message went).
_BUDGET = 4


_BANNED_PLATFORM = ("system", "machine", "release", "uname", "win32_ver", "mac_ver")


def _sites(roots=("bin", "m3_memory"), base: Path = _ROOT) -> tuple[list[str], list[str]]:
    checks, banned = [], []
    for root in roots:
        for p in sorted((base / root).rglob("*.py")):
            if p == _SEAM or p.relative_to(base).as_posix() in EXEMPT:
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            rel = p.relative_to(base).as_posix()
            # Resolve import aliases (`import sys as _sys`): an alias must not
            # carry an OS check past the gate. `from sys import platform` and
            # `from platform import system` are the same checks by another name.
            mods = {"os": "os", "sys": "sys", "platform": "platform"}
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        if a.name in ("os", "sys", "platform"):
                            mods[a.asname or a.name] = a.name
                elif isinstance(n, ast.ImportFrom) and n.module in ("os", "sys", "platform"):
                    for a in n.names:
                        if (n.module, a.name) in (("os", "name"), ("sys", "platform")):
                            checks.append(f"{rel}:{n.lineno}")
                        elif n.module == "platform" and a.name in _BANNED_PLATFORM:
                            banned.append(f"{rel}:{n.lineno} platform.{a.name}")
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)):
                    continue
                mod = mods.get(n.value.id)
                if (mod, n.attr) in (("os", "name"), ("sys", "platform")):
                    checks.append(f"{rel}:{n.lineno}")
                elif mod == "platform" and n.attr in _BANNED_PLATFORM:
                    banned.append(f"{rel}:{n.lineno} platform.{n.attr}")
    return checks, banned


def test_no_wmi_prone_platform_calls_outside_the_seam():
    _, banned = _sites()
    assert not banned, (
        "platform.* outside m3_memory/_platform.py (WMI hang on Windows + 3.14); "
        "use os_name()/is_windows()/is_apple_silicon():\n  " + "\n  ".join(banned)
    )


def test_os_checks_outside_the_seam_do_not_grow():
    checks, _ = _sites()
    assert len(checks) <= _BUDGET, (
        f"OS checks outside the seam rose from {_BUDGET} to {len(checks)}. Ask the OS seam "
        "(m3_memory/_platform.py) instead, adding the fact there if it is missing."
    )


def test_the_budget_is_not_stale():
    checks, _ = _sites()
    assert len(checks) == _BUDGET, f"OS checks outside the seam are down to {len(checks)}; lower _BUDGET to lock it in"


# Tests follow the seam too: an OS-specific test carries an OS marker
# (requires_windows / requires_posix / requires_macos / requires_linux /
# requires_apple_silicon, probed through the seam in conftest) or asks the seam.
# 70 checks at adoption; 25 single-comparison skipifs became markers.
# 2026-10-16: 45 -> 51 is a MEASUREMENT correction, not growth: the gate now
# resolves import aliases and sees conftest's `import os as _os` sites (5 of
# them the platform-identity restorer itself, 1 the Windows console guard).
_TESTS_BUDGET = 51


def test_os_checks_in_tests_do_not_grow():
    checks, banned = _sites(("tests",))
    assert not banned, f"platform.* in tests (WMI hang): {banned}"
    assert len(checks) <= _TESTS_BUDGET, (
        f"OS checks in tests rose from {_TESTS_BUDGET} to {len(checks)}; use an OS marker "
        "(@pytest.mark.requires_windows, ...) or the OS seam instead"
    )
    assert len(checks) == _TESTS_BUDGET, (
        f"OS checks in tests are down to {len(checks)}; lower _TESTS_BUDGET to lock it in"
    )


def test_the_gate_sees_each_form(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "m.py").write_text(
        "import os, sys, platform\na = os.name == 'nt'\nb = sys.platform == 'darwin'\nc = platform.system()\n",
        encoding="utf-8",
    )
    checks, banned = _sites(("bin",), tmp_path)
    assert checks == ["bin/m.py:2", "bin/m.py:3"] and banned == ["bin/m.py:4 platform.system"]


def test_the_gate_sees_through_import_aliases(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "m.py").write_text(
        "import sys as _sys\nimport platform as _pl\nfrom os import name\n"
        "a = _sys.platform == 'win32'\nb = _pl.machine()\n",
        encoding="utf-8",
    )
    checks, banned = _sites(("bin",), tmp_path)
    assert sorted(checks) == ["bin/m.py:3", "bin/m.py:4"] and banned == ["bin/m.py:5 platform.machine"]


def test_every_exemption_still_needs_its_own_check():
    """An exempt file that no longer tests the OS itself must leave EXEMPT, or
    the list silently grows a pass for a file that later regresses."""
    stale = []
    for rel in EXEMPT:
        p = _ROOT / rel
        if not p.is_file():
            stale.append(f"{rel} (missing)")
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        if not any(
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and (n.value.id, n.attr) in (("os", "name"), ("sys", "platform"))
            for n in ast.walk(tree)
        ):
            stale.append(rel)
    assert not stale, (
        f"observed: EXEMPT entries with no OS check of their own: {stale}. "
        "inspect: remove them from EXEMPT in tests/test_os_seam_ratchet.py"
    )
