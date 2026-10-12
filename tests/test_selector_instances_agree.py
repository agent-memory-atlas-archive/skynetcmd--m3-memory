"""Two selector instances in one process resolve the same backend.

`memory/backends/selector.py` can be loaded twice in one process. When it
memoized the resolved name in a module global, each instance kept its own memo:
the `pg` fixture reset the instance it held, the code under test read the other,
and PG-marked tests ran against SQLite under M3_DB_BACKEND=postgres.

The rival instance is loaded from FILE, not copied: a copied `__dict__` keeps
`__globals__` pointing at the original module, so the test could not fail.
`sys.modules` is never touched (see .claude/rules/test-sandbox.md).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from conftest import select_backend  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

_SRC = Path(__file__).resolve().parents[1] / "bin" / "memory" / "backends" / "selector.py"


def _rival_instance() -> ModuleType:
    """A separate selector instance with its own globals, NOT registered in
    sys.modules. The dotted name gives it `__package__ = "memory.backends"` so
    its relative imports resolve against the live package."""
    spec = importlib.util.spec_from_file_location("memory.backends.selector", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_reset_through_one_instance_is_seen_by_the_other(monkeypatch):
    """The exact shape of the fault: resolve under sqlite, flip to postgres,
    reset the instance the fixture holds, then read through the OTHER one."""
    held = importlib.import_module("memory.backends.selector")
    rival = _rival_instance()
    assert rival is not held and rival.__dict__ is not held.__dict__, "not a separate instance"

    select_backend("sqlite")
    assert held.resolve_backend_name() == "sqlite"
    assert rival.resolve_backend_name() == "sqlite"

    select_backend("postgres")
    held._reset_for_tests()

    assert held.resolve_backend_name() == "postgres"
    assert rival.resolve_backend_name() == "postgres", (
        "the rival instance still serves its earlier resolution — the seam would "
        "hand sqlite to code running under M3_DB_BACKEND=postgres"
    )


@pytest.mark.parametrize("first,second", [("sqlite", "postgres"), ("postgres", "sqlite")])
def test_a_selection_change_takes_effect_without_a_reset(first, second, monkeypatch):
    """No memo means no stale value to reset; installer._persist_pg_backend
    selects postgres in-process (a flag) and expects later steps to see it."""
    sel = importlib.import_module("memory.backends.selector")
    select_backend(first)
    assert sel.resolve_backend_name() == first
    select_backend(second)
    assert sel.resolve_backend_name() == second


def test_an_unrecognized_backend_still_raises(monkeypatch):
    """Dropping the memo must not drop the validation: a typo must never run SQLite.
    A flag is refused when it is set; one in the file, when it is resolved."""
    from m3_memory import _roots

    sel = importlib.import_module("memory.backends.selector")
    with pytest.raises(ValueError, match="postgre"):
        select_backend("postgre")
    cfg = _roots.config_root()
    import os

    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "m3_config.toml"), "w", encoding="utf-8", newline="\n") as f:
        f.write('schema_version = 1\n[db]\nbackend = "postgre"\n')
    from m3_core.config import settings as st

    st._reset_for_tests()
    from m3_core.config import ConfigFileError, format_config_error

    with pytest.raises(ConfigFileError) as refused:
        sel.resolve_backend_name()
    assert "postgre" in format_config_error(refused.value)
    st._reset_for_tests()
