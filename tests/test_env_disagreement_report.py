"""The transition report (user decision 2026-10-12, m3 64ea4eab): until the user
retires it, `m3 doctor` and setup name every env var that DISAGREES with a value
set in m3_config.toml, both values and which one m3 uses. An env var that agrees
is never reported (§3: a warning that fires when nothing is wrong trains people
to ignore it)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


@pytest.fixture
def config_file():
    from m3_core.config import settings as st

    from m3_memory import _roots

    root = Path(_roots.config_root())
    root.mkdir(parents=True, exist_ok=True)
    f = root / "m3_config.toml"

    def write(text: str) -> Path:
        f.write_text("schema_version = 1\n" + text, encoding="utf-8")
        st._reset_for_tests()
        return f

    yield write
    st._reset_for_tests()


def test_agreement_is_silent(config_file, monkeypatch):
    from m3_core.config.settings import env_disagreements

    config_file('[db]\nbackend = "postgres"\n[search]\nrow_cap = 50\n')
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_SEARCH_ROW_CAP", "50")
    assert env_disagreements() == []


def test_no_value_in_the_file_is_nothing_to_disagree_with(config_file, monkeypatch):
    from m3_core.config.settings import env_disagreements

    config_file("")
    monkeypatch.setenv("M3_SEARCH_ROW_CAP", "60")
    assert env_disagreements() == []


def test_the_file_wins_for_an_ordinary_key(config_file, monkeypatch):
    from m3_core.config.settings import env_disagreements

    config_file("[search]\nrow_cap = 50\n")
    monkeypatch.setenv("M3_SEARCH_ROW_CAP", "60")
    [d] = env_disagreements()
    assert (d.key, d.env_name, d.env_value, d.file_value) == ("search.row_cap", "M3_SEARCH_ROW_CAP", 60, 50)


def test_the_file_wins_for_the_backend_too(config_file, monkeypatch):
    """U9 reversed (m3 9cbed0af): db.backend is an ordinary key, so the file wins."""
    from m3_core.config.settings import env_disagreements
    from m3_core.paths import resolve_backend_name

    config_file('[db]\nbackend = "postgres"\n')
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    [d] = env_disagreements()
    assert (d.env_name, d.env_value, d.file_value) == ("M3_DB_BACKEND", "sqlite", "postgres")
    assert resolve_backend_name() == "postgres"


def test_doctor_names_both_values_and_says_the_file_is_used(config_file, monkeypatch, capsys):
    from m3_memory.install.sections import _config_env_disagreement_section

    config_file('[db]\nbackend = "postgres"\n')
    _config_env_disagreement_section()
    assert capsys.readouterr().out == "", "agreement (no env set) must print nothing"
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    _config_env_disagreement_section()
    out = capsys.readouterr().out
    assert "M3_DB_BACKEND=sqlite" in out and "db.backend = postgres" in out and "the file's value" in out, out
    assert "[i]" in out and "[!]" not in out, "a shadowed leftover is not a fault (§3)"


@pytest.mark.parametrize(("env", "warned"), [("0", True), ("1", False)])
def test_setup_names_an_env_var_that_contradicts_what_it_records(monkeypatch, tmp_path, capsys, env, warned):
    from m3_memory import setup_wizard

    monkeypatch.setenv("M3_ENABLE_OLLAMA_FAILOVER", env)  # the sandbox's config root takes the record
    setup_wizard._record_llm_switch("M3_ENABLE_OLLAMA_FAILOVER", "1")
    out = capsys.readouterr().out
    assert ("disagrees" in out) is warned, out
