"""Integrity of the three seams and their hierarchy (DESIGN_PHILOSOPHIES §10a).

Python version (m3_memory/_pyver.py) < OS (m3_memory/_platform.py) < database
(memory/backends) < business logic; imports point DOWN only. These tests check
the hierarchy (what each seam may import) and each seam's own contract. The
gates that keep call sites OUT of the seams' business are separate:
test_python_version_seam.py, test_os_seam_ratchet.py, test_scratchpad_filter.py.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import math
import platform
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

_ROOT = Path(__file__).resolve().parents[1]
_PYVER = _ROOT / "m3_memory" / "_pyver.py"
_PLATFORM = _ROOT / "m3_memory" / "_platform.py"
_ROOTS = _ROOT / "m3_memory" / "_roots.py"
_BACKENDS = _ROOT / "bin" / "memory" / "backends"

# What the database seam may reach outside itself in the memory package. Each is
# a database-level helper, imported lazily; anything else (search, write, entity,
# memory_core, ...) is business logic and would invert the hierarchy.
_DB_SEAM_ALLOWED_MEMORY = {
    "memory.db": "the connection factory the backends wrap",
    "memory.config": "settings the backends read",
    "memory.util": "vector math (_cosine_batch_packed)",
    # The two below are dialect-specific and belong INSIDE the seam; listed so
    # moving them is a visible to-do rather than a silent allowance.
}


def _imports(path: Path) -> set[str]:
    """Absolute module names a file imports (relative imports resolved)."""
    pkg = path.relative_to(_ROOT / "bin").with_suffix("").parts if "bin" in path.parts else ()
    out: set[str] = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = list(pkg[: len(pkg) - n.level])
                mod = ".".join(base + ([n.module] if n.module else []))
                out |= {f"{mod}.{a.name}" if not n.module else mod for a in n.names}
            elif n.module:
                out.add(n.module)
                # `from memory import config` imports the module memory.config
                out |= {f"{n.module}.{a.name}" for a in n.names}
    return out


def _top(names: set[str]) -> set[str]:
    return {m.split(".")[0] for m in names}


# ── the hierarchy ──────────────────────────────────────────────────────────────
def test_the_version_seam_is_stdlib_only():
    foreign = _top(_imports(_PYVER)) - set(sys.stdlib_module_names) - {"__future__"}
    assert not foreign, f"m3_memory/_pyver.py is layer 0 and may import only the stdlib: {foreign}"


def test_the_os_seam_imports_only_stdlib_and_the_version_seam():
    names = _imports(_PLATFORM)
    foreign = {
        m
        for m in names
        if m.split(".")[0] not in sys.stdlib_module_names and m != "__future__" and not m.startswith("m3_memory._pyver")
    }
    assert not foreign, f"m3_memory/_platform.py may import only the stdlib and _pyver: {foreign}"


def test_the_bin_path_owner_is_stdlib_only():
    """m3_memory/_bin.py runs before anything in bin/ can be imported."""
    foreign = _top(_imports(_ROOT / "m3_memory" / "_bin.py")) - set(sys.stdlib_module_names) - {"__future__"}
    assert not foreign, f"m3_memory/_bin.py may import only the stdlib: {foreign}"


# Files that put something OTHER than the payload bin/ on sys.path, by design.
_SYS_PATH_ALLOWED = {
    "m3_memory/_bin.py",  # the owner
    "m3_memory/team_cli.py",  # the examples/ orchestrator dir, not bin/
    "m3_memory/integrations/crewai/test_provider_logic.py",  # test harness: repo root
    "m3_memory/integrations/langchain/test_provider_logic.py",
    "m3_memory/integrations/pydantic_ai/test_provider_logic.py",
}


def test_m3_memory_puts_bin_on_the_path_only_through_its_owner():
    """About twenty m3_memory sites inserted bin/ themselves, three of them at
    parents[2]/bin, which is site-packages/bin on a wheel install (user decision
    2026-10-11: one owner, m3_memory._bin.ensure_on_path)."""
    hits = []
    for p in sorted((_ROOT / "m3_memory").rglob("*.py")):
        rel = p.relative_to(_ROOT).as_posix()
        if "__pycache__" in p.parts or rel in _SYS_PATH_ALLOWED:
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            f = n.func if isinstance(n, ast.Call) else None
            if (
                isinstance(f, ast.Attribute)
                and f.attr in ("insert", "append")
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "path"
                and getattr(f.value.value, "id", "") in ("sys", "_sys")
            ):
                hits.append(f"{rel}:{n.lineno}")
    assert not hits, "use m3_memory._bin.ensure_on_path(): " + ", ".join(hits)


def test_the_roots_owner_imports_only_stdlib_and_the_lower_seams():
    """m3_memory/_roots.py is imported by code that must run without m3_core (the
    installed package, the crypto layer, the chatlog hooks), so it may reach only
    the stdlib and the version/OS seams (user decision 2026-10-11)."""
    names = _imports(_ROOTS)
    allowed = ("m3_memory._pyver", "m3_memory._platform")
    foreign = {
        m
        for m in names
        if m.split(".")[0] not in sys.stdlib_module_names and m != "__future__" and not m.startswith(allowed)
    }
    assert not foreign, f"m3_memory/_roots.py may import only the stdlib, _pyver and _platform: {foreign}"


def test_the_database_seam_does_not_import_business_logic():
    leaks = []
    for p in sorted(_BACKENDS.glob("*.py")):
        for m in _imports(p):
            if m == "memory_core" or (m.startswith("memory.") and not m.startswith("memory.backends")):
                owner = next((a for a in _DB_SEAM_ALLOWED_MEMORY if m == a or m.startswith(a + ".")), None)
                if owner is None:
                    leaks.append(f"{p.name}: {m}")
    assert not leaks, "the database seam imports business logic (layer 2 may not reach up): " + ", ".join(leaks)


def test_the_db_seam_allowlist_is_not_stale():
    used = {m for p in _BACKENDS.glob("*.py") for m in _imports(p)}
    stale = [a for a in _DB_SEAM_ALLOWED_MEMORY if not any(m == a or m.startswith(a + ".") for m in used)]
    assert not stale, f"no longer imported by the database seam, drop from the allowlist: {stale}"


# ── each seam's contract ───────────────────────────────────────────────────────
def test_the_os_seam_names_exactly_one_os():
    pf = importlib.import_module("m3_memory._platform")
    name = pf.os_name()
    assert name in {"Windows", "Darwin", "Linux"}
    assert pf.is_windows() == (name == "Windows")
    if pf.is_apple_silicon():
        assert name == "Darwin"


def test_the_version_seam_describes_this_interpreter():
    pv = importlib.import_module("m3_memory._pyver")
    major, minor, _ = platform.python_version_tuple()
    assert pv.PY == (int(major), int(minor))
    assert pv.wheel_python_tag() == f"cp{major}{minor}"
    floor = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    assert platform.python_version() in SpecifierSet(floor), "running on an interpreter m3 does not support"
    slack = pv.event_loop_timer_slack()
    assert 0 <= slack < 1 and math.isfinite(slack)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_every_dialect_implements_the_whole_contract(backend):
    dl = importlib.import_module("memory.backends.dialect")
    concrete = type(dl.dialect_for(backend))

    def _declares_contract(fn) -> bool:
        try:
            return "raise NotImplementedError" in inspect.getsource(fn)
        except (OSError, TypeError):  # generated (dataclass) methods have no source
            return False

    abstract = [name for name, fn in inspect.getmembers(dl.Dialect, inspect.isfunction) if _declares_contract(fn)]
    assert abstract, "the base Dialect declares no contract methods; the probe is broken"
    missing = [n for n in abstract if getattr(concrete, n) is getattr(dl.Dialect, n)]
    assert not missing, f"{concrete.__name__} leaves base contract methods unimplemented: {missing}"


def test_bin_reaches_the_seams_only_through_m3_core_seams():
    """bin/ can run from a checkout on an interpreter without the m3_memory
    package (dev tooling, CI lint, the pre-push hook). m3_core.seams makes the
    package resolvable there; a bin/ module importing m3_memory._platform or
    _pyver directly fails in that layout before the resolver ever runs."""
    hits = []
    for p in sorted((_ROOT / "bin").rglob("*.py")):
        if "__pycache__" in p.parts or p.name == "seams.py":
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            mods = (
                [n.module, *(f"{n.module}.{a.name}" for a in n.names)]
                if isinstance(n, ast.ImportFrom) and n.module
                else [a.name for a in n.names]
                if isinstance(n, ast.Import)
                else []
            )
            if any(m in ("m3_memory._platform", "m3_memory._pyver", "m3_memory._roots") for m in mods):
                hits.append(f"{p.relative_to(_ROOT).as_posix()}:{n.lineno}")
    assert not hits, (
        "observed: bin/ imports a seam module directly: " + ", ".join(hits) + ". inspect: import it from m3_core.seams"
    )


def test_m3_core_seams_reexports_the_seams_unchanged():
    """The bin-side resolver re-exports; it must never become a second owner."""
    seams = importlib.import_module("m3_core.seams")
    pf = importlib.import_module("m3_memory._platform")
    pv = importlib.import_module("m3_memory._pyver")
    # The roots owner is re-exported as a module, so its functions stay its own.
    assert seams.roots is importlib.import_module("m3_memory._roots"), "m3_core.seams.roots is not m3_memory._roots"
    assert seams.childenv is importlib.import_module("m3_memory._childenv"), "m3_core.seams.childenv is not its owner"
    for name in (n for n in seams.__all__ if n not in ("roots", "childenv")):
        owner = pf if hasattr(pf, name) else pv
        assert getattr(seams, name) is getattr(owner, name), f"m3_core.seams.{name} is not the seam's own"
    defs = [
        n.name
        for n in ast.parse((_ROOT / "bin" / "m3_core" / "seams.py").read_text(encoding="utf-8")).body
        if isinstance(n, (ast.FunctionDef, ast.ClassDef))
    ]
    assert not defs, f"m3_core.seams defines logic of its own: {defs}"


def test_business_logic_asks_the_dialect_to_compile_keyword_queries():
    """FTS5 vs tsquery syntax is the database seam's (Dialect.compile_keyword_query).
    memory.fts re-exports the compilers for old importers; nothing else names them."""
    owners = {"bin/memory/fts.py", "bin/memory_core.py"}
    hits = []
    for p in sorted((_ROOT / "bin").rglob("*.py")) + sorted((_ROOT / "m3_memory").rglob("*.py")):
        rel = p.relative_to(_ROOT).as_posix()
        if "__pycache__" in p.parts or rel in owners or rel.startswith("bin/memory/backends/"):
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.ImportFrom) and any(
                a.name in ("_compile_fts_query", "_compile_tsquery") for a in n.names
            ):
                hits.append(f"{rel}:{n.lineno}")
    assert not hits, (
        "observed: business logic imports a keyword compiler: "
        + ", ".join(hits)
        + ". inspect: call dialect().compile_keyword_query(query, mode)"
    )


def test_the_user_environment_primitives_agree():
    """user_env_snapshot / user_env_get: one store, one answer. Either the OS has
    a persistent per-user environment (Windows) and both read it, or neither
    does and both say so; no OS-name test, the capability decides."""
    pf = importlib.import_module("m3_memory._platform")
    snap = pf.user_env_snapshot()
    if snap is None:
        assert pf.user_env_get("PATH") is None
        return
    for name, (value, _kind) in list(snap.items())[:5]:
        assert pf.user_env_get(name) == str(value), name
    assert pf.user_env_get("M3_SURELY_UNSET_7F3A") is None
