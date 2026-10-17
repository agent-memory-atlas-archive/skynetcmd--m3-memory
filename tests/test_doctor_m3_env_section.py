"""`m3 doctor` ends with the m3 settings still set as M3_* variables.

m3 reads its settings from m3_config.toml and one variable, M3_CONFIG_ROOT, only
when it is not the default (user decision 2026-10-17). Every other M3_* setting
is reported as deprecated, with the `m3 config set` that replaces it. Names and
places only, never values. The sweep is faked: these tests read nothing real.
"""

from __future__ import annotations

import importlib

import pytest

from m3_memory.install import env_sources, sections
from m3_memory.install.env_sources import EnvFound


class _Settings:
    def __init__(self, file_keys=()):
        self._file = set(file_keys)

    def origin(self, key):
        return "file:/x/m3_config.toml" if key.name in self._file else "env:M3_X"


@pytest.fixture
def sweep(monkeypatch):
    settings_mod = importlib.import_module("m3_core.config.settings")

    def use(found, file_keys=()):
        monkeypatch.setattr(env_sources, "scan", lambda names, **k: list(found))
        monkeypatch.setattr(settings_mod, "current", lambda: _Settings(file_keys))

    return use


def test_a_setting_in_the_environment_is_reported_with_its_fix(sweep, capsys):
    sweep([EnvFound("M3_ENGINE_ROOT", "/secret/place", "~/.zshenv", "rc")])
    assert sections.m3_env_settings_section(brief=True) == 1
    out = capsys.readouterr().out
    assert "[deprecated] M3_ENGINE_ROOT -> paths.engine_root (in use); set in ~/.zshenv" in out
    assert "m3 config set paths.engine_root <value>" in out
    assert "m3 config import-env" in out and "Remove the ignored" not in out
    assert "/secret/place" not in out, "values are never printed"


def test_one_the_file_already_sets_is_marked_ignored(sweep, capsys):
    sweep(
        [EnvFound("M3_ENGINE_ROOT", "/x", "environment (os.environ['M3_ENGINE_ROOT'])", "process")],
        {"paths.engine_root"},
    )
    sections.m3_env_settings_section(brief=True)
    out = capsys.readouterr().out
    assert "(ignored, m3_config.toml sets it)" in out and "fix:" not in out
    assert "Remove the ignored ones" in out and "import-env" not in out


def test_another_tools_mcp_entry_is_not_reported(sweep, capsys):
    sweep([EnvFound("M3_ENGINE_ROOT", "/x", "~/.gemini/settings.json", "mcp", server="other", m3_server=False)])
    assert sections.m3_env_settings_section(brief=True) == 0
    assert capsys.readouterr().out == ""


def test_m3_config_root_at_the_default_is_called_redundant(sweep, monkeypatch, capsys):
    from m3_memory import _roots

    default = _roots.default_config_root()
    monkeypatch.setattr(importlib.import_module("m3_core.paths"), "get_m3_config_root", lambda: default)
    sweep([EnvFound("M3_CONFIG_ROOT", default, "~/.zshrc", "rc")])
    sections.m3_env_settings_section(brief=True)
    out = capsys.readouterr().out
    assert "M3_CONFIG_ROOT is set to the default in ~/.zshrc" in out


def test_nothing_set_prints_nothing(sweep, capsys):
    sweep([])
    assert sections.m3_env_settings_section() == 0
    assert capsys.readouterr().out == ""


def test_a_keychain_lookup_line_is_not_called_a_plaintext_copy(monkeypatch, capsys):
    """MacBook 2026-10-17: `export X=$(security find-generic-password ...)` holds no
    value on disk; it exports one into every shell. Saying "plaintext copy" was wrong."""
    from types import SimpleNamespace

    ss = importlib.import_module("m3_core.secret_store")
    monkeypatch.setattr(ss, "explain_named_secret", lambda n: SimpleNamespace(value="resolved"))
    monkeypatch.setattr(
        env_sources,
        "scan",
        lambda names, **k: [
            EnvFound("LM_API_TOKEN", "$(security find-generic-password)", "~/.zshenv", "rc", computed=True)
        ],
    )
    sections._secrets_in_env_section(brief=False)
    out = capsys.readouterr().out
    assert "lookup at startup (no value stored there)" in out
    assert "plaintext copies" not in out
