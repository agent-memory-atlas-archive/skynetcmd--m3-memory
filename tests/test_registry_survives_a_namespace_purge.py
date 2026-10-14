"""A held registry reference still resolves when it is not the live instance.

`@register_backend` resolves `register_backend` through
`sys.modules["memory.backends.registry"]`, so registration lands in whichever
registry module is live. Code holding an EARLIER instance — anything that
captured a registry function before that module was replaced — saw an empty
`_REGISTRY` and raised "no dialect registered" for a backend that is shipped,
allow-listed and importable.

Deliberately does NOT purge the `memory.*` namespace.
`memory.*` sys.modules manipulation leaks across tests (13
unrelated failures in 2026-07, 141 on the Windows PostgreSQL lane in 2026-10,
all as `'module' object is not callable` from the shadowed `dialect` symbol).
The package object is never replaced here; only the single
`memory.backends.registry` key is swapped, inside a monkeypatch context.
"""

from __future__ import annotations

import contextlib
import gc
import importlib
import sys
from pathlib import Path
from types import FunctionType, ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

_BACKEND_MODULES = ("memory.backends.sqlite_backend", "memory.backends.postgres_backend")


@contextlib.contextmanager
def _restored_backends():
    """Undo the reload this file provokes.

    `_ensure_registered` adopts a live registration by `importlib.reload` of the
    backend module, which re-executes it IN PLACE: same module object, new class
    and function objects. Anything that cached the earlier ones keeps them, so
    later tests ran a mix of copies (three `keyword_search_with_row_data` were
    alive after this file; search_profile's timing broke on it). Restoring each
    module's namespace puts the originals back for everything after us.
    """
    saved = {name: dict(sys.modules[name].__dict__) for name in _BACKEND_MODULES if name in sys.modules}
    try:
        yield
    finally:
        for name, namespace in saved.items():
            mod = sys.modules.get(name)
            if mod is not None:
                mod.__dict__.clear()
                mod.__dict__.update(namespace)


@pytest.fixture(autouse=True)
def _restore_backend_modules():
    with _restored_backends():
        yield


def _live_copies() -> int:
    gc.collect()
    return sum(
        1
        for o in gc.get_objects()
        # type(), not isinstance: isinstance falls back to o.__class__,
        # and some objects (a deprecated torch alias) warn when it is read.
        if type(o) is FunctionType and o.__qualname__ == "SqliteBackend.keyword_search_with_row_data"
    )


def _second_instance() -> ModuleType:
    """A genuinely separate `memory.backends.registry` instance.

    Copying a module's `__dict__` is NOT enough: the copied functions keep
    `__globals__` pointing at the original module, so `@register_backend` would
    still write into the original `_REGISTRY` and the test could not fail.
    Loading the file afresh gives the new module its own globals and its own
    registry, which is the condition this is about.
    """
    import importlib.util

    src = Path(__file__).resolve().parents[1] / "bin" / "memory" / "backends" / "registry.py"
    spec = importlib.util.spec_from_file_location("memory.backends.registry", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_a_held_registry_adopts_a_live_registration(backend, monkeypatch):
    from memory.backends import registry as held

    held._ensure_registered(backend)
    assert backend in held._REGISTRY, "precondition: the backend is registered"

    with monkeypatch.context() as m:
        # The held module is no longer the live one; the live one has the entry.
        live = _second_instance()
        m.setattr(held, "_REGISTRY", {}, raising=False)
        m.setitem(sys.modules, "memory.backends.registry", live)

        assert held._REGISTRY == {}, "precondition: the held registry is empty"
        assert live._REGISTRY is not held._REGISTRY, "not a separate instance"
        # Not object identity: re-running @register_backend builds an equal
        # entry. What matters is that the HELD registry ends up able to answer.
        dialect = held.dialect_singleton_for(backend)
        assert dialect.backend == backend, dialect
        assert held._REGISTRY.get(backend) is not None, "nothing was adopted"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_backend_factory_for_shares_the_adoption(backend, monkeypatch):
    """`backend_factory_for` uses the same `_ensure_registered`."""
    from memory.backends import registry as held

    held._ensure_registered(backend)
    assert backend in held._REGISTRY, "precondition: the backend is registered"

    with monkeypatch.context() as m:
        live = _second_instance()
        m.setattr(held, "_REGISTRY", {}, raising=False)
        m.setitem(sys.modules, "memory.backends.registry", live)
        assert callable(held.backend_factory_for(backend))


def test_the_dialect_accessor_is_untouched_by_all_of_this():
    """The package object is never replaced, so the accessor stays callable."""
    B = importlib.import_module("memory.backends")

    assert callable(B.dialect), type(B.dialect).__name__


def test_the_reload_leaves_no_backend_copy_behind(monkeypatch):
    """Measured the way the defect was found: count the live copies of one
    backend method before and after an adoption that reloads the module."""
    from memory.backends import registry as held

    held._ensure_registered("sqlite")
    import memory.backends.sqlite_backend  # noqa: F401 - the module the reload replaces

    before = _live_copies()
    with _restored_backends(), monkeypatch.context() as m:
        m.setattr(held, "_REGISTRY", {}, raising=False)
        m.setitem(sys.modules, "memory.backends.registry", _second_instance())
        held.dialect_singleton_for("sqlite")
        assert _live_copies() > before, "precondition: the adoption reloaded the module"
    assert _live_copies() == before, "a reloaded copy of the backend outlived the test"
