"""<dynamic> env sites, design Q3: the odd ones read
the registry, or stop reading env at all."""

from __future__ import annotations

import json

import pytest


def test_get_setting_is_deprecated_and_reads_the_registry(m3_settings, monkeypatch, caplog):
    from m3_core import context
    from m3_core.context import M3Context

    monkeypatch.setattr(context, "_GET_SETTING_WARNED", False)
    m3_settings({"search.row_cap": 7})
    ctx = object.__new__(M3Context)  # get_setting touches no instance state
    with caplog.at_level("WARNING", logger="memory_core"):
        assert ctx.get_setting("search.row_cap") == 7
        assert ctx.get_setting("NOT_AN_M3_NAME", "dflt") == "dflt"
    notices = [r.getMessage() for r in caplog.records if "get_setting" in r.getMessage()]
    assert len(notices) == 1 and "settings.setting(name)" in notices[0], notices


def test_get_setting_takes_an_env_name_of_a_key(m3_settings):
    from m3_core.config import REGISTRY
    from m3_core.context import M3Context

    m3_settings({"search.row_cap": 9})
    env_name = REGISTRY.get("search.row_cap").env_names[0]
    assert object.__new__(M3Context).get_setting(env_name) == 9


def test_the_default_user_id_is_a_setting(m3_settings):
    from m3_memory.integrations.langchain import mapping

    m3_settings()
    assert mapping.resolve_user_id(None) is None
    m3_settings({"identity.default_user_id": "solo"})
    assert mapping.resolve_user_id(None) == "solo"
    assert mapping.resolve_user_id("explicit", "ctor") == "explicit"


def test_pg_restore_is_a_setting(m3_settings, tmp_path):
    from memory.backends import postgres_backend as pb

    tool = tmp_path / "pg_restore"
    tool.write_text("", encoding="utf-8")
    m3_settings({"db.postgres.pg_restore": tool})
    assert pb._pg_tool("pg_restore") == str(tool.absolute())
    m3_settings({"db.postgres.pg_restore": tmp_path / "missing"})
    with pytest.raises(pb.SnapshotError, match=r"db\.postgres\.pg_restore"):
        pb._pg_tool("pg_restore")


def test_installer_config_records_no_env(monkeypatch, tmp_path):
    from m3_memory import installer

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path)
    monkeypatch.setenv("M3_FIPS_MODE", "1")
    installer.save_config({"version": "x"})
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {"version": "x"}
