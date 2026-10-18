"""LLM endpoint switches reach daemons that never read the shell rc: setup and
install record them in m3_config.toml [llm], read back through the registry.
.llm_config.json is no longer a value source (its parse is kept for import-env)."""

import argparse
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from m3_core import llm_config  # noqa: E402

from m3_memory import setup_wizard  # noqa: E402


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


def _ollama_on() -> bool:
    from m3_core.config import settings as st

    st._reset_for_tests()  # the snapshot watches m3_config.toml, not this file
    return st.setting("llm.ollama_failover")


def test_file_is_used_when_the_environment_is_bare(config_root):
    llm_config.write_llm_setting("M3_ENABLE_OLLAMA_FAILOVER", "1")
    assert _ollama_on() is True


def test_the_environment_is_not_read(config_root, monkeypatch):
    monkeypatch.setenv("M3_ENABLE_OLLAMA_FAILOVER", "1")
    assert _ollama_on() is False


def test_malformed_file_is_reported(config_root, caplog):
    (config_root / llm_config.LLM_CONFIG_NAME).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="M3_SDK"):
        assert llm_config.read_llm_config() == {}
    assert "unreadable" in caplog.text


def test_absent_file_is_silent(config_root, caplog):
    with caplog.at_level(logging.WARNING, logger="M3_SDK"):
        assert llm_config.read_llm_config() == {}
    assert caplog.text == ""


def _no_detected_llm(monkeypatch):
    """These tests cover the local-port switches: no detected server is offered."""
    from m3_memory.install import llm_detect

    monkeypatch.setattr(llm_detect, "find_candidates", lambda found=None: [])


def _probe(monkeypatch, reachable_port):
    _no_detected_llm(monkeypatch)
    monkeypatch.setattr(setup_wizard, "_endpoint_reachable", lambda url, **k: reachable_port in url)
    setup_wizard._probe_llm_endpoints(object(), argparse.Namespace(non_interactive=True))


def _setting(name):
    """What a freshly started daemon reads (the [llm] switches are not reloadable:
    a running process keeps its startup value until restart, by design)."""
    from m3_core.config import settings as st

    st._reset_for_tests()
    return st.setting(name)


def test_setup_records_ollama_even_when_the_shell_already_has_it(config_root, monkeypatch):
    # The repair path for an existing install: the shell exports the switch, which
    # m3 does not read, so setup must still record it where a daemon reads it.
    monkeypatch.setenv("M3_ENABLE_OLLAMA_FAILOVER", "1")
    _probe(monkeypatch, "11434")
    assert _setting("llm.ollama_failover") is True
    assert _setting("llm.lmstudio_failover") is False
    assert not (config_root / llm_config.LLM_CONFIG_NAME).exists(), "nothing goes to the legacy file"


def test_setup_records_a_fresh_ollama_enable(config_root, monkeypatch):
    _probe(monkeypatch, "11434")
    assert _setting("llm.ollama_failover") is True
    text = (config_root / "m3_config.toml").read_text(encoding="utf-8")
    assert "ollama_failover = true" in text
    # The file only: setup no longer writes env, the registry, rc or HKCU.
    assert "M3_ENABLE_OLLAMA_FAILOVER" not in os.environ


def test_setup_keeps_a_custom_server_url_from_the_file(config_root, monkeypatch, capsys):
    llm_config.write_llm_setting("M3_LLM_URL", "http://localhost:8080/v1")
    before = (config_root / "m3_config.toml").read_bytes()
    monkeypatch.setattr(setup_wizard, "_endpoint_reachable", lambda url, **k: True)
    setup_wizard._probe_llm_endpoints(object(), argparse.Namespace(non_interactive=True))
    assert "llm.url set (http://localhost:8080/v1)" in capsys.readouterr().out
    assert (config_root / "m3_config.toml").read_bytes() == before, "a pinned URL is confirmed, not rewritten"


def test_an_env_only_custom_url_is_not_read(config_root, monkeypatch):
    monkeypatch.setenv("M3_LLM_URL", "http://localhost:8080/v1")
    assert not _setting("llm.url")


def test_an_unknown_name_is_refused(config_root):
    with pytest.raises(ValueError, match="not the former name"):
        llm_config.write_llm_setting("M3_NOT_A_SETTING", "1")


def test_setup_writes_nothing_when_only_the_default_is_reachable(config_root, monkeypatch):
    _probe(monkeypatch, "1234")
    assert not (config_root / llm_config.LLM_CONFIG_NAME).exists()


@pytest.mark.requires_posix  # shell rc files are POSIX
@pytest.mark.parametrize(
    "text, expected",
    [
        ('export M3_ENABLE_OLLAMA_FAILOVER="1"\n', True),
        ("export M3_ENABLE_OLLAMA_FAILOVER=1  # m3\n", True),
        ('# export M3_ENABLE_OLLAMA_FAILOVER="1"\n', False),
        ('export M3_ENABLE_OLLAMA_FAILOVER="1"\nexport M3_ENABLE_OLLAMA_FAILOVER=0\n', False),
        ("export M3_ENABLE_OLLAMA_FAILOVER=10\n", False),
    ],
)
def test_shell_rc_has_reads_the_effective_assignment(monkeypatch, tmp_path, text, expected):
    from m3_memory.wizard import persist

    rc = tmp_path / ".zshrc"
    rc.write_text(text, encoding="utf-8")
    monkeypatch.setattr(persist, "_pick_unix_shell_rc", lambda: rc)
    assert persist.shell_rc_has("M3_ENABLE_OLLAMA_FAILOVER", "1") is expected


@pytest.mark.parametrize("from_upgrade", [True, False])
def test_no_runtime_guidance_is_for_setup_not_the_upgrade(monkeypatch, capsys, from_upgrade):
    """The full how-to-add-a-model guidance is for setup. Under `m3 upgrade` a
    host that never had a runtime is the standing state: nothing is printed
    (it was six lines, then one, on every run). A runtime that WAS enabled and
    went away is a change, covered in test_upgrade_output_and_cache_retry."""
    _no_detected_llm(monkeypatch)
    monkeypatch.setattr(setup_wizard, "_endpoint_reachable", lambda url, **k: False)
    monkeypatch.setattr(setup_wizard, "_llm_switch_enabled", lambda var: False)
    if from_upgrade:
        monkeypatch.setattr("m3_memory.wizard.ui._caller", "upgrade")
    else:
        monkeypatch.setattr("m3_memory.wizard.ui._caller", None)
    setup_wizard._probe_llm_endpoints(object(), argparse.Namespace(non_interactive=True))
    out = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    if from_upgrade:
        assert out == []
    else:
        assert any("no LLM found" in ln for ln in out)
        assert any("OLLAMA_HOST" in ln for ln in out), "the guidance names how to point at another machine"
        assert len(out) > 1, out
