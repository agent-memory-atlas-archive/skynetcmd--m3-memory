""".llm_config.json, like the legacy env names and .env, is no longer a value
source: m3_config.toml > default. A value set there is reported (Resolved.shadowed,
`m3 config show`, `m3 doctor`) and never read; setup copies it into a NEW
m3_config.toml once. One reader: llm_failover reads setting()."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

URL = "llm.url"


def _key(name):
    from m3_core.config import REGISTRY

    return REGISTRY.get(name)


def _build(tmp_path, *, toml=None, environ=None, dotenv=None, llmfile=None):
    from m3_core.config import Location
    from m3_core.config.resolve import DotEnv
    from m3_core.config.settings import build

    p = tmp_path / "m3_config.toml"
    if toml is not None:
        p.write_text(toml, encoding="utf-8", newline="\n")
    return build(
        Location(file=p, root=tmp_path, tier="t"),
        environ=environ or {},
        dotenv=DotEnv(path=str(tmp_path / ".env"), values=dotenv or {}),
        llmfile=DotEnv(path=str(tmp_path / ".llm_config.json"), values=llmfile or {}, kind="llmfile"),
    )


def test_only_the_file_and_the_default_give_a_value(tmp_path):
    k = _key(URL)
    legacy = {
        "environ": {"M3_LLM_URL": "http://env/v1"},
        "dotenv": {"M3_LLM_URL": "http://dotenv/v1"},
        "llmfile": {"M3_LLM_URL": "http://llmfile/v1"},
    }
    (tmp_path / "with").mkdir()
    (tmp_path / "without").mkdir()
    s = _build(tmp_path / "with", toml='[llm]\nurl = "http://toml/v1"\n', **legacy)
    assert s[k] == "http://toml/v1"
    s = _build(tmp_path / "without", **legacy)
    assert s[k] is None and s.origin(k) == "default"


def test_an_llm_file_value_is_reported_and_not_read(tmp_path):
    k = _key(URL)
    s = _build(tmp_path, llmfile={"M3_LLM_URL": "http://localhost:8080/v1"})
    assert s[k] is None and s.origin(k) == "default"
    assert s.shadowed_env() == [f"M3_LLM_URL (in {tmp_path / '.llm_config.json'})"]


def test_every_legacy_source_still_set_is_reported(tmp_path):
    s = _build(tmp_path, dotenv={"M3_LLM_URL": "http://dotenv/v1"}, llmfile={"M3_LLM_URL": "http://llmfile/v1"})
    assert s.shadowed_env() == sorted(
        [f"M3_LLM_URL (in {tmp_path / '.env'})", f"M3_LLM_URL (in {tmp_path / '.llm_config.json'})"]
    )


def test_a_bad_llm_file_value_is_not_read_so_it_is_not_an_error(tmp_path):
    k = _key("llm.ollama_failover")
    s = _build(tmp_path, llmfile={"M3_ENABLE_OLLAMA_FAILOVER": "maybe"})
    assert s[k] is False
    assert any(".llm_config.json" in n for n in s.shadowed_env())


@pytest.fixture
def config_root():
    """The sandbox's per-test config root; the sandbox also clears the LLM env names."""
    from m3_core.config import settings as st

    from m3_memory import _roots

    root = Path(_roots.config_root())
    root.mkdir(parents=True, exist_ok=True)
    st._reset_for_tests()
    yield root
    st._reset_for_tests()


def _write(root, data):
    (root / ".llm_config.json").write_text(json.dumps(data), encoding="utf-8")


def test_the_real_loader_reads_the_file_under_the_config_root(config_root):
    """JSON booleans read as the words the env parser takes; nothing is injected."""
    from m3_core.config import env as cfg_env

    _write(config_root, {"M3_ENABLE_OLLAMA_FAILOVER": True, "M3_LLM_URL": " http://x/v1 ", "M3_NOTHING": None})
    path, values = cfg_env.llm_file()
    assert Path(path) == config_root / ".llm_config.json"
    assert values == {"M3_ENABLE_OLLAMA_FAILOVER": "true", "M3_LLM_URL": "http://x/v1"}
    assert "M3_ENABLE_OLLAMA_FAILOVER" not in os.environ


def test_an_absent_or_malformed_file_adds_nothing(config_root, caplog):
    from m3_core.config import env as cfg_env

    assert cfg_env.llm_file() == (None, {})
    assert caplog.text == ""
    (config_root / ".llm_config.json").write_text("{not json", encoding="utf-8")
    assert cfg_env.llm_file() == (None, {})
    assert "unreadable" in caplog.text, "a malformed file is reported (§3), not silently empty"


def test_setting_does_not_read_the_file(config_root, tmp_path):
    from m3_core.config import settings as st

    _write(config_root, {"M3_ENABLE_OLLAMA_FAILOVER": "1"})
    assert st.setting("llm.ollama_failover") is False
    assert st.origin_of("llm.ollama_failover") == "default"


@pytest.mark.parametrize(
    "values, want",
    [
        ({}, ["http://localhost:1234/v1"]),
        ({"llm.url": "http://box:8080/v1"}, ["http://box:8080/v1"]),
        (
            {"llm.url": "http://box:8080/v1", "llm.lmstudio_failover": True},
            ["http://box:8080/v1", "http://localhost:1234/v1"],
        ),
        ({"llm.ollama_failover": True}, ["http://localhost:1234/v1", "http://localhost:11434/v1"]),
        ({"llm.lmstudio_failover": False}, []),
        (
            {"llm.endpoints": ["http://a/v1", "http://b/v1"], "llm.ollama_failover": True},
            ["http://a/v1", "http://b/v1"],
        ),
    ],
)
def test_llm_failover_builds_its_list_from_the_registry(m3_settings, values, want):
    import llm_failover

    m3_settings(values)
    assert llm_failover.endpoints_from_settings() == want


def test_llm_failover_ignores_the_file_as_the_registry_does(config_root):
    import llm_failover

    _write(config_root, {"M3_LLM_URL": "http://box:8080/v1", "M3_ENABLE_OLLAMA_FAILOVER": "1"})
    assert llm_failover.endpoints_from_settings() == ["http://localhost:1234/v1"]


def test_config_show_lists_the_llm_file_value_as_not_read(config_root):
    import config_cli

    _write(config_root, {"M3_LLM_URL": "http://box:8080/v1"})
    out = config_cli.show(config_cli.build(), origin=True, as_json=False)
    assert "not read" in out and "M3_LLM_URL (in " in out
    data = json.loads(config_cli.show(config_cli.build(), origin=True, as_json=True))
    assert any(n.startswith("M3_LLM_URL (in ") for n in data["ignored_env"])
    assert "llm_file_in_use" not in data


def test_doctor_names_the_llm_file_value_to_delete(config_root, capsys):
    from m3_memory.install.sections import m3_env_settings_section

    _write(config_root, {"M3_LLM_URL": "http://box:8080/v1"})
    from m3_core.config import settings as st

    st._reset_for_tests()
    m3_env_settings_section()
    out = capsys.readouterr().out
    assert "M3_LLM_URL" in out and ".llm_config.json" in out and "not read" in out
