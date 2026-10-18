"""The session-end guard that fails a run when a real agent config names the
session's pytest temp area (a test wrote the developer's config; it went unseen
from 2026-10-17 until the config-root doctor check found it). Proven here to fire
on a planted leak and to stay quiet on an unrelated pytest path."""

from __future__ import annotations

import json

import conftest


def test_the_guard_fires_on_a_config_naming_the_basetemp(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    basetemp = tmp_path / "basetemp"
    leaked = home / ".gemini" / "settings.json"
    leaked.write_text(json.dumps({"env": {"M3_CONFIG_ROOT": f"{basetemp.as_posix()}/t0/config"}}), encoding="utf-8")
    monkeypatch.setattr(conftest, "_REAL_HOME", str(home))
    assert conftest._leaked_into_real_configs(basetemp) == [str(leaked)]


def test_the_guard_ignores_another_sessions_temp_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text('{"x": "C:/Temp/pytest-of-u/pytest-1/old"}', encoding="utf-8")
    monkeypatch.setattr(conftest, "_REAL_HOME", str(home))
    assert conftest._leaked_into_real_configs(tmp_path / "basetemp") == []
