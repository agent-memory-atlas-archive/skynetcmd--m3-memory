"""Named stores (CONFIG_DESIGN §14; plan 2C.3; user decisions D1-D3, m3 dfeb41b7).

A store is ``(backend, locator)`` in ``m3_config.toml``::

    [stores.bench]
    backend = "postgres"
    dsn_secret = "M3_STORE_BENCH_PG_URL"   # a secret NAME, never a DSN in the file
    [stores.scratch]
    backend = "sqlite"
    path = "~/.m3/engine/scratch.db"

``main`` is synthesised from today's settings (db.backend, the primary DSN or the
default SQLite path) and may not be declared until 2D moves every reader to the
registry: a declared [stores.main] would disagree with the default the
unconverted readers still use. ``active_store(name)`` selects a store for a block;
the resolvers in m3_core.paths then follow it (§14 order, D3). The chatlog stays
process-wide (D1). A store's files part follows it (3c).
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from m3_core.paths import SELECTABLE_BACKENDS, StoreRef, _active_store

MAIN = "main"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")

# Each backend's locator keys: the ONE declaration a new backend adds here
# (test_config_stores pins it to SELECTABLE_BACKENDS, so a backend cannot be
# selectable without a locator). value: required.
LOCATOR_KEYS: dict[str, dict[str, bool]] = {
    "sqlite": {"path": True},
    "postgres": {"dsn_secret": True},
}


class StoreError(ValueError):
    """A store that cannot be resolved or used."""


def parse_stores(table: object, problem) -> dict[str, StoreRef]:
    """Validate a ``[stores]`` table; report each problem through ``problem(key,
    message)`` (the loader's error collector) and return the valid stores."""
    out: dict[str, StoreRef] = {}
    if not isinstance(table, dict):
        problem("stores", f"must be a table of [stores.<name>] tables, found {type(table).__name__}")
        return out
    for name, spec in table.items():
        where = f"stores.{name}"
        if name == MAIN:
            problem(where, "main is derived from db.backend and the primary DSN or the default "
                           "SQLite path until 2D; give this store another name")
            continue
        if not _NAME.fullmatch(name):
            problem(where, "a store name is lowercase letters, digits, '_' or '-', starting with a letter")
            continue
        if not isinstance(spec, dict):
            problem(where, f"must be a table, found {type(spec).__name__}")
            continue
        backend = spec.get("backend")
        if backend not in SELECTABLE_BACKENDS:
            problem(f"{where}.backend", f"{backend!r} is not one of {', '.join(SELECTABLE_BACKENDS)}")
            continue
        allowed = {"backend", *LOCATOR_KEYS[backend]}
        for extra in sorted(set(spec) - allowed):
            problem(f"{where}.{extra}", f"not a {backend} store key (allowed: {', '.join(sorted(allowed))})")
        missing = [k for k, req in LOCATOR_KEYS[backend].items() if req and k not in spec]
        for m in missing:
            problem(f"{where}.{m}", f"a {backend} store needs {m}")
        bad = [k for k in LOCATOR_KEYS[backend] if k in spec and not isinstance(spec[k], str)]
        for k in bad:
            problem(f"{where}.{k}", f"must be a string, found {type(spec[k]).__name__}")
        if missing or bad or set(spec) - allowed:
            continue
        if backend == "sqlite":
            out[name] = StoreRef(name=name, backend=backend,
                                 path=str(Path(spec["path"]).expanduser().absolute()))
        else:
            out[name] = StoreRef(name=name, backend=backend, dsn_secret=spec["dsn_secret"])
    return out


def configured_stores() -> Mapping[str, StoreRef]:
    from .settings import current

    return current().stores


def resolve_store(name: str) -> StoreRef | None:
    """The named store; None for ``main`` (meaning: no store override). A name not
    configured is an error with a did-you-mean."""
    if name == MAIN:
        return None
    stores = configured_stores()
    store = stores.get(name)
    if store is None:
        near = difflib.get_close_matches(name, list(stores), n=1, cutoff=0.6)
        hint = f"; did you mean {near[0]!r}?" if near else ""
        known = ", ".join(sorted([MAIN, *stores])) or MAIN
        raise StoreError(f"observed: store {name!r} is not configured (configured: {known}){hint}. "
                         "inspect: [stores.<name>] in m3_config.toml (`m3 config show --origin`)")
    return store


@contextmanager
def active_store(name: str | None) -> Iterator[StoreRef | None]:
    """Use store ``name`` for this block (None: keep whatever is active; ``main``:
    the default store, overriding an outer store). The resolvers in m3_core.paths
    follow it: resolve_backend_name, and resolve_db_path for a SQLite store."""
    if name is None:
        yield _active_store.get()
        return
    store = resolve_store(name)
    token = _active_store.set(store)
    try:
        yield store
    finally:
        _active_store.reset(token)


def current_store_name() -> str:
    store = _active_store.get()
    return store.name if store is not None else MAIN
