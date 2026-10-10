"""`store_is_a_file()` is the one owner of "does a path select the store?".

Five tools answered it themselves with three fallbacks; reembed_space said True
on ANY error, so a misconfigured PostgreSQL install silently took the SQLite
file path. Pins the backend fact, the resolver, and its two failure modes.
"""
from __future__ import annotations

import builtins
import importlib

import pytest


def test_every_registered_backend_declares_it():
    from m3_core.paths import SELECTABLE_BACKENDS
    from memory.backends import registry

    for name in SELECTABLE_BACKENDS:
        registry._ensure_registered(name)
        cls = registry._REGISTRY[name].backend_factory
        assert isinstance(getattr(cls, "stores_databases_as_files", None), bool), (
            f"observed: backend {name!r} does not declare stores_databases_as_files. "
            f"inspect: memory/backends/base.py StorageBackend")


def test_resolver_follows_the_active_backend(each_backend):
    from m3_core.paths import store_is_a_file

    assert store_is_a_file() is each_backend.stores_databases_as_files
    assert store_is_a_file() is (each_backend.name == "sqlite"), f"observed: on {each_backend.name}"


@pytest.mark.parametrize("name, expected", [("sqlite", True), ("postgres", False)])
def test_bootstrap_answers_from_the_configured_name(monkeypatch, name, expected):
    paths = importlib.import_module("m3_core.paths")
    real_import = builtins.__import__

    def no_memory(mod, *a, **kw):
        if mod == "memory.backends":
            raise ImportError("bootstrap: no memory package")
        return real_import(mod, *a, **kw)

    monkeypatch.setenv("M3_DB_BACKEND", name)
    monkeypatch.setattr(builtins, "__import__", no_memory)
    assert paths.store_is_a_file() is expected


@pytest.mark.parametrize("resolver", ["seam_backend", "seam_dialect"])
def test_a_refused_config_is_not_answered_with_sqlite(monkeypatch, resolver):
    """PostgreSQL with no DSN is refused by the seam ("Will not silently fall
    back to SQLite"); the bootstrap resolvers caught Exception and handed back
    the SQLite shim anyway (measured 2026-10-10)."""
    mb = importlib.import_module("memory.backends")
    paths = importlib.import_module("m3_core.paths")

    def refused():
        raise RuntimeError("observed: PostgreSQL backend selected but no DSN")

    monkeypatch.setattr(mb, "active_backend", refused)
    monkeypatch.setattr(mb, "dialect", refused)
    with pytest.raises(RuntimeError, match="no DSN"):
        getattr(paths, resolver)()


@pytest.mark.parametrize("resolver", ["seam_backend", "seam_dialect"])
def test_bootstrap_shim_only_for_sqlite(monkeypatch, resolver):
    paths = importlib.import_module("m3_core.paths")
    real_import = builtins.__import__

    def no_memory(mod, *a, **kw):
        if mod == "memory.backends":
            raise ImportError("bootstrap: no memory package")
        return real_import(mod, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_memory)
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    assert getattr(paths, resolver)().param() if resolver == "seam_dialect" else getattr(paths, resolver)().name
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    with pytest.raises(RuntimeError, match="observed: M3_DB_BACKEND=postgres"):
        getattr(paths, resolver)()


def test_a_seam_fault_is_not_read_as_a_file(monkeypatch):
    mb = importlib.import_module("memory.backends")
    paths = importlib.import_module("m3_core.paths")

    def broken():
        raise RuntimeError("seam misconfigured")

    monkeypatch.setattr(mb, "active_backend", broken)
    with pytest.raises(RuntimeError, match="seam misconfigured"):
        paths.store_is_a_file()
