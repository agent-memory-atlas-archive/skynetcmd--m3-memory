"""`m3 config get|set|unset` (plan 2F.1 slice 1; design 2026.10.12-IMPORT_ENV_DESIGN):
CONFIG keys only, parsed by the key's own parser, written under the §12 lock; a
security-sensitive key needs a person at the console or --yes."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    import config_cli

    f = tmp_path / "m3_config.toml"
    monkeypatch.setattr(config_cli, "console_has_person", lambda: False)
    return f


def _run(*argv):
    import config_cli

    return config_cli.main(list(argv))


def _file(f):
    return tomllib.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def test_set_then_get_round_trips_in_the_toml_type(cfg, capsys):
    assert _run("set", "search.row_cap", "700", "--config", str(cfg)) == 0
    assert _file(cfg)["search"]["row_cap"] == 700, "an int, not the string '700'"
    capsys.readouterr()
    assert _run("get", "search.row_cap", "--config", str(cfg)) == 0
    assert capsys.readouterr().out.strip() == "700"


def test_a_list_is_written_as_an_array(cfg):
    assert _run("set", "llm.endpoints", "http://a/v1, http://b/v1", "--config", str(cfg)) == 0
    assert _file(cfg)["llm"]["endpoints"] == ["http://a/v1", "http://b/v1"]


def test_a_bad_value_is_refused_naming_it_and_the_file_is_untouched(cfg, capsys):
    assert _run("set", "search.row_cap", "lots", "--config", str(cfg)) == 2
    err = capsys.readouterr().err
    assert "search.row_cap" in err and "'lots'" in err
    assert not cfg.exists()


def test_an_unknown_key_is_refused(cfg, capsys):
    assert _run("set", "search.row_capp", "7", "--config", str(cfg)) == 2
    assert "not a setting" in capsys.readouterr().err and not cfg.exists()


def test_a_secret_key_is_never_written_here(cfg, capsys):
    from m3_core.config import REGISTRY, KeyClass

    secret = next(k for k in REGISTRY if k.klass is KeyClass.SECRET)
    assert _run("set", secret.name, "x", "--config", str(cfg)) == 2
    assert "secret" in capsys.readouterr().err and not cfg.exists()


def test_a_sensitive_key_without_a_person_or_yes_is_refused(cfg, capsys):
    assert _run("set", "db.backend", "postgres", "--config", str(cfg)) == 2
    assert "--yes" in capsys.readouterr().err and not cfg.exists()
    assert _run("set", "db.backend", "postgres", "--yes", "--config", str(cfg)) == 0
    assert _file(cfg)["db"]["backend"] == "postgres"


def test_a_sensitive_key_asks_a_person_and_no_writes_nothing(cfg, monkeypatch, capsys):
    import config_cli

    monkeypatch.setattr(config_cli, "console_has_person", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert _run("set", "paths.engine_root", "/srv/m3", "--config", str(cfg)) == 1
    assert not cfg.exists()
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert _run("set", "paths.engine_root", "/srv/m3", "--config", str(cfg)) == 0
    assert _file(cfg)["paths"]["engine_root"] == "/srv/m3"


def test_unset_removes_the_key_and_get_reports_the_fallback(cfg, capsys):
    _run("set", "search.row_cap", "700", "--config", str(cfg))
    assert _run("unset", "search.row_cap", "--config", str(cfg)) == 0
    assert "row_cap" not in _file(cfg).get("search", {})
    capsys.readouterr()
    _run("get", "search.row_cap", "--origin", "--config", str(cfg))
    out = capsys.readouterr().out
    assert "# " in out and "file:" not in out


def test_get_origin_names_the_file(cfg, capsys):
    _run("set", "search.row_cap", "700", "--config", str(cfg))
    capsys.readouterr()
    _run("get", "search.row_cap", "--origin", "--config", str(cfg))
    assert f"file:{cfg}" in capsys.readouterr().out


def test_set_reports_an_env_var_that_contradicts_the_new_value(cfg, monkeypatch, capsys):
    """The transition report: set says
    when an env var disagrees with what it just wrote."""
    monkeypatch.setenv("M3_SEARCH_ROW_CAP", "800")
    assert _run("set", "search.row_cap", "700", "--config", str(cfg)) == 0
    out = capsys.readouterr().out
    assert "M3_SEARCH_ROW_CAP=800" in out and "m3 uses 700 (the file)" in out and "m3 doctor" in out


def test_m3_config_set_dispatches_end_to_end(tmp_path):
    import subprocess

    f = tmp_path / "m3_config.toml"
    out = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", "--config", str(f), "config", "set", "search.row_cap", "7"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert _file(f)["search"]["row_cap"] == 7


def test_confirmation_follows_the_registry_not_a_list():
    """A new LOCATOR key is confirmed without editing config_cli."""
    import config_cli
    from m3_core.config import REGISTRY
    from m3_core.config.registry import Scope

    for key in REGISTRY:
        expected = key.name == "db.backend" or key.scope is Scope.LOCATOR
        assert config_cli.needs_confirmation(key) is expected, key.name
    assert not config_cli.needs_confirmation(REGISTRY.get("search.row_cap"))


def test_console_has_person_lives_in_the_os_seam():
    from m3_memory import _platform
    from m3_memory.wizard import ui

    assert ui.console_has_person is _platform.console_has_person
