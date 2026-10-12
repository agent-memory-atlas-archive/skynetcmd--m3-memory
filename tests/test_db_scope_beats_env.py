"""A call scoped with ``active_database()`` resolves its scoped DB, whatever
``M3_DATABASE`` says (M3_DATABASE design, approved 2026-10-12, phase 1; plan
Step 2E.2, R5).

The env value used to outrank the ContextVar, so a scoped call that "worked"
might only have worked because env happened to name the same file: a scope that
was never set looked correct. These tests set env to a DIFFERENT file, so a
scope that is not honoured fails loud instead of agreeing by accident.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def two_dbs(tmp_path, monkeypatch):
    scoped = tmp_path / "scoped.db"
    elsewhere = tmp_path / "elsewhere.db"
    monkeypatch.setenv("M3_DATABASE", str(elsewhere))
    return os.path.abspath(scoped), os.path.abspath(elsewhere)


def test_the_scope_beats_env(two_dbs):
    from m3_core.paths import active_database, resolve_db_path

    scoped, elsewhere = two_dbs
    assert resolve_db_path() == elsewhere, "with no scope the legacy env still answers"
    with active_database(scoped):
        assert resolve_db_path() == scoped
    assert resolve_db_path() == elsewhere


def test_an_explicit_argument_beats_the_scope(two_dbs, tmp_path):
    from m3_core.paths import active_database, resolve_db_path

    scoped, _ = two_dbs
    explicit = os.path.abspath(tmp_path / "explicit.db")
    with active_database(scoped):
        assert resolve_db_path(explicit) == explicit


def test_the_scope_survives_the_async_bridge(two_dbs):
    """run_blocking runs on a worker thread when a loop is already running; the
    worker must see the caller's scope, not env."""
    import asyncio

    from m3_core.aio import run_blocking
    from m3_core.paths import active_database, resolve_db_path

    scoped, _ = two_dbs

    async def resolve():
        return resolve_db_path()

    async def caller():
        with active_database(scoped):
            return run_blocking(resolve)

    assert asyncio.run(caller()) == scoped


def test_a_dsn_from_the_scope_names_the_scope(two_dbs, monkeypatch):
    """The refusal names which knob held the DSN, and never echoes the password."""
    from m3_core.paths import _active_db, resolve_db_path

    monkeypatch.delenv("M3_DATABASE")
    token = _active_db.set("postgresql://u:hunter2pw@h/db")
    try:
        with pytest.raises(ValueError) as e:
            resolve_db_path()
    finally:
        _active_db.reset(token)
    # A distinctive password, not the word 'secret': the message legitimately says
    # "stored secret", and a false alarm on a security check is a defect (IDIOMS §5).
    assert "active_database() context" in str(e.value) and "hunter2pw" not in str(e.value)


def test_access_stamps_land_in_the_store_that_was_read(two_dbs, monkeypatch, tmp_path):
    """The stamp flusher is a task created by the FIRST reader and keeps that
    reader's context for good; ids alone sent a scoped store's stamps to the
    wrong database, where they matched nothing and were lost."""
    import contextlib
    import importlib

    from m3_core.paths import active_database, resolve_db_path

    mdb = importlib.import_module("memory.db")  # not `from memory import db`: the namespace purge

    other = os.path.abspath(tmp_path / "other.db")
    seen: list[tuple[str, tuple]] = []

    class _Conn:
        def execute(self, sql, params):
            seen.append((resolve_db_path(), tuple(params[1:])))

        def commit(self):
            pass

    @contextlib.contextmanager
    def fake_db():
        yield _Conn()

    monkeypatch.setattr(mdb, "_db", fake_db)
    monkeypatch.setattr(mdb, "_access_pending", set())
    scoped, _ = two_dbs
    with active_database(scoped):
        mdb._access_pending.update((str(resolve_db_path(None)), i) for i in ("a", "b"))
    with active_database(other):
        mdb._access_pending.update((str(resolve_db_path(None)), i) for i in ("c",))
    assert mdb.flush_access_stamps_now() == 3
    by_store = {store: set(ids) for store, ids in seen}
    assert by_store == {scoped: {"a", "b"}, other: {"c"}}, seen
