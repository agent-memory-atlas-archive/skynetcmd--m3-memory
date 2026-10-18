"""An env var that disagrees with m3_config.toml changes nothing: m3 reads the
file (its env names are not read). ``env_contradicting`` is what `m3 config set`
and setup use to say so -- it names each env var set to something OTHER than the
value recorded, and never one that agrees (§3: a warning that fires when nothing
is wrong trains people to ignore it). doctor's general report of M3_* names that
are not read is m3_env_settings_section (tests/test_doctor_m3_env_section.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


@pytest.fixture
def config_file():
    """Append TOML to the sandbox's m3_config.toml (keeping its [paths], which
    pin every root under tmp) and drop the cached snapshot."""
    from m3_core.config import settings as st

    from m3_memory import _roots

    f = Path(_roots.config_root()) / "m3_config.toml"
    base = f.read_text(encoding="utf-8")

    def write(text: str) -> Path:
        f.write_text(base + "\n" + text, encoding="utf-8")
        st._reset_for_tests()
        return f

    yield write
    f.write_text(base, encoding="utf-8")
    st._reset_for_tests()


def _key(name):
    from m3_core.config import REGISTRY

    return REGISTRY.get(name)


def test_agreement_is_silent(config_file, monkeypatch):
    from m3_core.config.settings import env_contradicting

    config_file('[db]\nbackend = "postgres"\n[search]\nrow_cap = 50\n')
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_SEARCH_ROW_CAP", "50")
    assert env_contradicting(_key("db.backend"), "postgres") == []
    assert env_contradicting(_key("search.row_cap"), 50) == []


def test_the_file_wins_for_an_ordinary_key(config_file, monkeypatch):
    from m3_core.config.settings import env_contradicting, setting

    config_file("[search]\nrow_cap = 50\n")
    monkeypatch.setenv("M3_SEARCH_ROW_CAP", "60")
    assert setting("search.row_cap") == 50
    assert env_contradicting(_key("search.row_cap"), 50) == [("M3_SEARCH_ROW_CAP", 60)]


def test_the_file_wins_for_the_backend_too(config_file, monkeypatch):
    """db.backend is an ordinary key, so the file wins."""
    from m3_core.config.settings import env_contradicting
    from m3_core.paths import resolve_backend_name

    config_file('[db]\nbackend = "postgres"\n')
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    assert resolve_backend_name() == "postgres"
    assert env_contradicting(_key("db.backend"), "postgres") == [("M3_DB_BACKEND", "sqlite")]


def test_an_env_value_with_no_file_value_is_not_read(config_file, monkeypatch):
    """No value in the file: the default is used, never the env var."""
    from m3_core.config.settings import setting

    config_file("")
    monkeypatch.setenv("M3_SEARCH_ROW_CAP", "60")
    assert setting("search.row_cap") == _key("search.row_cap").default


@pytest.mark.parametrize("env", ["0", "1"])
def test_setup_records_the_switch_and_the_env_var_changes_nothing(monkeypatch, tmp_path, capsys, env):
    """setup writes the switch into m3_config.toml; a leftover env var is not read,
    so it is not argued with here (setup's M3_* section names it, to delete)."""
    from m3_core.config import settings as st
    from m3_core.config.settings import setting

    from m3_memory import setup_wizard

    monkeypatch.setenv("M3_ENABLE_OLLAMA_FAILOVER", env)  # the sandbox's config root takes the record
    setup_wizard._record_llm_switch("M3_ENABLE_OLLAMA_FAILOVER", "1")
    out = capsys.readouterr().out
    assert "disagrees" not in out, out
    st._reset_for_tests()
    assert setting("llm.ollama_failover") is True
