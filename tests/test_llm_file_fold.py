""".llm_config.json folds into the registry as its lowest legacy source (fold
step 1-3; plan 2026.10.12-LLM_CONFIG_FOLD_DESIGN): m3_config.toml > legacy env >
.env > .llm_config.json > default. One reader: llm_failover reads setting()."""

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


def test_each_tier_outranks_the_one_below(tmp_path):
    k = _key(URL)
    tiers = {
        "toml": '[llm]\nurl = "http://toml/v1"\n',
        "environ": {"M3_LLM_URL": "http://env/v1"},
        "dotenv": {"M3_LLM_URL": "http://dotenv/v1"},
        "llmfile": {"M3_LLM_URL": "http://llmfile/v1"},
    }
    expect = ["http://toml/v1", "http://env/v1", "http://dotenv/v1", "http://llmfile/v1", None]
    names = list(tiers)
    for i, want in enumerate(expect):
        (tmp_path / str(i)).mkdir()
        s = _build(tmp_path / str(i), **{n: tiers[n] for n in names[i:]})
        assert s[k] == want, (names[i:], s.origin(k))


def test_the_llm_file_alone_gives_todays_value_and_names_itself(tmp_path):
    k = _key(URL)
    s = _build(tmp_path, llmfile={"M3_LLM_URL": "http://localhost:8080/v1"})
    assert s[k] == "http://localhost:8080/v1"
    assert s.origin(k) == f"llmfile:{tmp_path / '.llm_config.json'} M3_LLM_URL"
    assert s.llm_file_in_use() == ["M3_LLM_URL"]


def test_an_outranked_llm_file_value_is_reported_shadowed(tmp_path):
    s = _build(tmp_path, dotenv={"M3_LLM_URL": "http://dotenv/v1"}, llmfile={"M3_LLM_URL": "http://llmfile/v1"})
    assert s.shadowed_env() == [f"M3_LLM_URL (in {tmp_path / '.llm_config.json'})"]
    assert s.llm_file_in_use() == [], "the value in use came from .env, not the LLM file"


def test_a_bad_llm_file_value_names_the_file(tmp_path):
    from m3_core.config import format_config_error
    from m3_core.config.resolve import ConfigEnvErrors

    with pytest.raises(ConfigEnvErrors) as exc:
        _build(tmp_path, llmfile={"M3_ENABLE_OLLAMA_FAILOVER": "maybe"})
    text = format_config_error(exc.value)
    assert ".llm_config.json" in text and ".env" not in text and "fix or remove the line" in text


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


def test_setting_reads_the_file_and_an_explicit_environ_does_not(config_root, tmp_path):
    from m3_core.config import settings as st

    _write(config_root, {"M3_ENABLE_OLLAMA_FAILOVER": "1"})
    assert st.setting("llm.ollama_failover") is True
    assert st.origin_of("llm.ollama_failover").startswith("llmfile:")
    hermetic = st.build(environ={})
    assert hermetic[_key("llm.ollama_failover")] is False, "a test's own environ stays hermetic"


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


def test_llm_failover_and_the_registry_agree_on_the_file(config_root):
    import llm_failover

    _write(config_root, {"M3_LLM_URL": "http://box:8080/v1", "M3_ENABLE_OLLAMA_FAILOVER": "1"})
    assert llm_failover.endpoints_from_settings() == ["http://box:8080/v1", "http://localhost:11434/v1"]


def test_config_show_lists_the_llm_file_as_legacy(config_root):
    import config_cli

    _write(config_root, {"M3_LLM_URL": "http://box:8080/v1"})
    out = config_cli.show(config_cli.build(), origin=True, as_json=False)
    assert "legacy .llm_config.json" in out and "#   M3_LLM_URL" in out
    data = json.loads(config_cli.show(config_cli.build(), origin=True, as_json=True))
    assert data["llm_file_in_use"] == ["M3_LLM_URL"]


def test_doctor_lists_the_llm_file_as_legacy_and_is_silent_without_it(config_root, capsys):
    from m3_memory.install.sections import _llm_file_legacy_section

    _llm_file_legacy_section()
    assert capsys.readouterr().out == ""
    _write(config_root, {"M3_LLM_URL": "http://box:8080/v1"})
    from m3_core.config import settings as st

    st._reset_for_tests()
    _llm_file_legacy_section()
    out = capsys.readouterr().out
    assert "M3_LLM_URL" in out and ".llm_config.json" in out and "[llm]" in out
