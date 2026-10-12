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


def test_a_dsn_from_the_scope_names_the_scope():
    """The refusal names which knob held the DSN, and never echoes the password."""
    from m3_core.paths import _active_db, resolve_db_path

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


# ── phase 2: the --database pin replaces m3's own M3_DATABASE writes ─────────
def test_the_pin_ranks_where_the_env_value_did(tmp_path, monkeypatch):
    """explicit > scope > pin > legacy env > default: the pin sits exactly where
    m3's own env writes sat, so callers see no change."""
    from m3_core.paths import active_database, pin_database, resolve_db_path

    pinned = os.path.abspath(tmp_path / "pinned.db")
    legacy = os.path.abspath(tmp_path / "legacy.db")
    scoped = os.path.abspath(tmp_path / "scoped.db")
    monkeypatch.setenv("M3_DATABASE", legacy)
    pin_database(pinned)
    assert resolve_db_path() == pinned, "the pin outranks the legacy env value"
    with active_database(scoped):
        assert resolve_db_path() == scoped, "a call's scope outranks the pin"
    assert "M3_DATABASE" in os.environ and os.environ["M3_DATABASE"] == legacy, "the pin wrote env"


def test_the_legacy_env_value_warns_once_and_the_pin_never(tmp_path, monkeypatch, caplog):
    import logging

    from m3_core import paths

    monkeypatch.setattr(paths, "_LEGACY_DB_WARNED", False)
    paths.pin_database(tmp_path / "pinned.db")
    with caplog.at_level(logging.WARNING):
        paths.configured_main_db()
    assert "M3_DATABASE" not in caplog.text, "the pin is not the legacy env var"
    paths.pin_database(None)
    monkeypatch.setenv("M3_DATABASE", str(tmp_path / "legacy.db"))
    with caplog.at_level(logging.WARNING):
        paths.configured_main_db()
        paths.configured_main_db()
    said = [r for r in caplog.records if "M3_DATABASE" in r.getMessage()]
    assert len(said) == 1 and "--database PATH" in said[0].getMessage(), said


def test_scoped_db_env_restores_the_pin_and_never_touches_env(tmp_path, monkeypatch):
    from m3_core.paths import configured_main_db, pin_database, scoped_db_env

    pin_database(tmp_path / "main.db")
    with scoped_db_env(tmp_path / "chat.db") as prev:
        assert prev == str(tmp_path / "main.db")
        assert configured_main_db() == str(tmp_path / "chat.db")
        assert "M3_DATABASE" not in os.environ
    assert configured_main_db() == str(tmp_path / "main.db")


def test_a_database_pin_does_not_move_a_pinned_chatlog(tmp_path, monkeypatch):
    """The trap a ContextVar would have sprung: on a split install the chatlog
    path pinned in .chatlog_config.json outranks a script's --database, as it
    outranked the M3_DATABASE env write that the pin replaces."""
    import json

    import chatlog_config
    from m3_core.paths import pin_database

    cfg = tmp_path / ".chatlog_config.json"
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(cfg))
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)  # the root conftest's; it outranks both
    chat = os.path.abspath(tmp_path / "agent_chatlog.db")
    main = os.path.abspath(tmp_path / "agent_memory.db")
    pin_database(main)
    chatlog_config.invalidate_cache()
    assert chatlog_config.chatlog_db_path() == main, "unpinned: the chatlog unifies with --database"
    cfg.write_text(json.dumps({"db_path": chat}), encoding="utf-8")
    chatlog_config.invalidate_cache()
    assert chatlog_config.chatlog_db_path() == chat, "a pinned chatlog moved onto --database"
