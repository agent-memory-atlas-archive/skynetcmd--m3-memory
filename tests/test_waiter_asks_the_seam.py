"""The waiter must ask the SEAM which lane it is on, never infer it.

#163's Rule-10a requirement is "ask the seam, do not infer". The waiter now
asks for a CAPABILITY -- `dialect().store_change_token()` -- not the backend name. Reading
`M3_DB_BACKEND` directly looked equivalent and was not: the selector resolves
through `getenv_compat("M3_DB_BACKEND", "DB_BACKEND", ...)`, so the still-
supported `DB_BACKEND` alias made the two disagree. Measured 2026-09-12::

    SEAM resolves to  : postgres
    WAITER would take : SQLITE WAL path

That is the §3 failure in its worst form. The waiter would watch
`agent_memory.db-wal` -- a file PostgreSQL never writes -- and poll forever
finding nothing, with no error and no log line: a blind waiter that looks
perfectly healthy.
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import m3_notification_waiter as waiter  # noqa: E402
from memory.backends import selector  # noqa: E402

from conftest import select_backend  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_backend_env(monkeypatch):
    monkeypatch.delenv("DB_BACKEND", raising=False)
    selector._reset_for_tests()
    yield
    selector._reset_for_tests()


def test_waiter_agrees_with_the_seam_on_postgres():
    """THE regression: when the seam says PostgreSQL the waiter must not take
    the SQLite WAL path (it would watch a .db-wal file PostgreSQL never writes
    and poll forever, with no error)."""
    select_backend("postgres")
    selector._reset_for_tests()
    assert selector.resolve_backend_name() == "postgres"
    assert not waiter._has_change_signal("agent_memory.db")


@pytest.mark.parametrize("var", ["M3_DB_BACKEND", "DB_BACKEND"])
def test_former_backend_env_names_move_neither_the_seam_nor_the_waiter(var, monkeypatch):
    """The former names are not read: seam and waiter agree on the default."""
    monkeypatch.setenv(var, "postgres")
    selector._reset_for_tests()
    assert selector.resolve_backend_name() == "sqlite"
    assert waiter._has_change_signal("agent_memory.db")


def test_default_is_the_sqlite_wal_path():
    """The common case must not regress into spawning a PG child."""
    selector._reset_for_tests()
    assert waiter._has_change_signal("agent_memory.db")


def test_supervisor_imports_no_db_framework_at_module_level():
    """#163: the supervisor MUST stay stdlib-only at import time to survive
    `pipx upgrade`. Deferring the seam import inside `_has_change_signal` is
    what keeps that true -- this guard fails if one drifts to the top."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(waiter.__file__).read_text(encoding="utf-8"))
    top_level = []
    for node in tree.body:  # module level ONLY, not ast.walk
        if isinstance(node, ast.Import):
            top_level += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level.append(node.module.split(".")[0])

    std = set(sys.stdlib_module_names)
    non_std = sorted(m for m in top_level if m not in std)
    assert non_std == [], (
        f"the supervisor gained top-level non-stdlib import(s) {non_std}; it "
        f"must stay stdlib-only at import time to survive `pipx upgrade` (#163)"
    )


def test_seam_failure_degrades_loudly_not_silently(monkeypatch, capsys):
    """If the seam cannot be imported we fall back to the configured backend name
    -- but that degradation must be logged. A silent fallback is how the original
    bug hid."""
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name.startswith("memory"):
            raise ImportError("simulated stripped payload")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    warned = []
    monkeypatch.setattr(waiter, "_warn", lambda m: warned.append(m))

    select_backend("postgres")
    assert not waiter._has_change_signal("agent_memory.db"), "the fallback ignored db.backend"
    assert warned, "the seam failed and nothing was logged -- silent degradation"


def _seam_unimportable(monkeypatch, *, also_m3_core=False):
    import builtins

    real_import = builtins.__import__
    blocked = ("memory", "m3_core") if also_m3_core else ("memory",)

    def boom(name, *a, **k):
        if name.startswith(blocked):
            raise ImportError("simulated stripped payload")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    monkeypatch.setattr(waiter, "_warn", lambda m: None)


@pytest.mark.parametrize("name", ["mariadb", "postgre"])
def test_seam_failure_sends_only_sqlite_to_the_file_lane(name, monkeypatch):
    """A future backend or a typo must not get the SQLite file-signal lane, where
    it would watch files nobody writes. The config file refuses an unknown
    backend at load, so the fallback raises (naming the line) rather than
    choosing a lane."""
    from m3_core.config import locate
    from m3_core.config import settings as st
    from m3_core.config.loader import ConfigFileError

    cfg = locate().file
    with open(cfg, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f'\n[db]\nbackend = "{name}"\n')
    st._reset_for_tests()
    _seam_unimportable(monkeypatch)
    with pytest.raises(ConfigFileError) as e:
        waiter._has_change_signal("agent_memory.db")
    assert any(name in str(x) and "backend" in str(x) for x in e.value.exceptions)


@pytest.mark.parametrize("also_m3_core", [False, True])
def test_seam_failure_keeps_sqlite_on_the_file_lane(also_m3_core, monkeypatch):
    """The fail-closed default the docstring defends: an unset backend (SQLite)
    stays on the file-signal lane, with or without m3_core importable."""
    _seam_unimportable(monkeypatch, also_m3_core=also_m3_core)
    assert waiter._has_change_signal("agent_memory.db")
