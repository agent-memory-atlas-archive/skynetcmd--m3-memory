"""The chatlog store has one configured owner (chatlog design, user decision
2026-10-13): ``pin_chatlog_db`` (a script's --chatlog-db) or, until N+3, one of the
legacy chatlog env names, read in one place. Every reader asks it.
"""

from __future__ import annotations

import pytest

_NAMES = ("M3_CHATLOG_DB_PATH", "CHATLOG_DB_PATH", "M3_CHATLOG_DB", "CHATLOG_DB", "M3_CHATLOG_DATABASE")


@pytest.fixture
def clean(monkeypatch):
    from m3_core import paths

    for n in _NAMES:
        monkeypatch.delenv(n, raising=False)
    paths.pin_chatlog_db(None)
    yield paths
    paths.pin_chatlog_db(None)


def test_nothing_configured_is_none(clean):
    assert clean.configured_chatlog_db() is None


@pytest.mark.parametrize("name", _NAMES)
def test_each_legacy_name_is_honoured(clean, monkeypatch, name):
    monkeypatch.setenv(name, f"/x/{name}.db")
    assert clean.configured_chatlog_db() == f"/x/{name}.db"


def test_the_pin_outranks_every_legacy_name(clean, monkeypatch):
    for n in _NAMES:
        monkeypatch.setenv(n, f"/x/{n}.db")
    clean.pin_chatlog_db("/pinned/chat.db")
    assert clean.configured_chatlog_db() == "/pinned/chat.db"


def test_the_order_among_legacy_names(clean, monkeypatch):
    monkeypatch.setenv("M3_CHATLOG_DATABASE", "/c.db")
    monkeypatch.setenv("CHATLOG_DB", "/b.db")
    assert clean.configured_chatlog_db() == "/b.db"
    monkeypatch.setenv("CHATLOG_DB_PATH", "/a.db")
    assert clean.configured_chatlog_db() == "/a.db"


def test_chatlog_config_reads_the_owner(clean, monkeypatch):
    """M3_CHATLOG_DATABASE used to reach only the batch scripts; through the one
    reader it reaches chatlog_config too, so every tool agrees on the store."""
    import chatlog_config

    monkeypatch.setenv("M3_CHATLOG_DATABASE", "/via/owner.db")
    chatlog_config.invalidate_cache()
    assert chatlog_config.chatlog_db_path() == "/via/owner.db"


def test_a_legacy_name_warns_once_with_the_fix(clean, monkeypatch, caplog):
    monkeypatch.setattr(clean, "_LEGACY_CHATLOG_WARNED", False)  # the sandbox marks it given
    monkeypatch.setenv("CHATLOG_DB", "/x/c.db")
    with caplog.at_level("WARNING", logger=clean.logger.name):
        clean.configured_chatlog_db()
        clean.configured_chatlog_db()
    warned = [r.getMessage() for r in caplog.records if "chatlog store comes from" in r.getMessage()]
    assert len(warned) == 1, warned
    assert "CHATLOG_DB" in warned[0] and "--chatlog-db" in warned[0] and "db_path" in warned[0]
    # No advice to move to another env name (the old getenv_compat notice did that).
    assert "use M3_" not in caplog.text


def test_the_pin_does_not_warn(clean, monkeypatch, caplog):
    monkeypatch.setattr(clean, "_LEGACY_CHATLOG_WARNED", False)
    clean.pin_chatlog_db("/pinned/chat.db")
    with caplog.at_level("WARNING", logger=clean.logger.name):
        assert clean.configured_chatlog_db() == "/pinned/chat.db"
    assert "chatlog store comes from" not in caplog.text


def test_the_chatlog_names_are_not_renamed_to_another_env_name():
    """doctor --fix used to rewrite CHATLOG_DB_PATH -> M3_CHATLOG_DB_PATH: moving a
    user from one env name to another. The fix is a flag or the config file."""
    from m3_core.paths import DEPRECATED_ENV_RENAMES

    assert "CHATLOG_DB_PATH" not in DEPRECATED_ENV_RENAMES
    assert "CHATLOG_DB" not in DEPRECATED_ENV_RENAMES


def test_migrate_memory_takes_chatlog_db():
    import migrate_memory

    args = migrate_memory.build_parser().parse_args(["--chatlog-db", "/x/c.db", "status"])
    assert args.chatlog_db == "/x/c.db"
    assert "M3_CHATLOG_DATABASE" not in migrate_memory._TARGET_HELP


def test_resolve_role_db_rejects_an_unknown_role(clean):
    with pytest.raises(ValueError, match="role must be"):
        clean.resolve_role_db(None, "M3_DATABASE", "agent_memory.db")
