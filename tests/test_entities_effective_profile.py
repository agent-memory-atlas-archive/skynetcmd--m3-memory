"""The entity pass applies the key rule to the endpoint it actually calls.

The shipped entity profile is Anthropic-format on a local port (LM Studio). A
local profile follows the configured LLM (slm_intent.localize_endpoint), which
calls it in OpenAI format, and that needs no key. The key was checked on the raw
profile, so on a machine with Ollama and no key stored the pass exited, and the
loop with it.
"""

from __future__ import annotations

import dataclasses

import m3_entities
import pytest
import slm_intent


def _local_anthropic_profile():
    prof = slm_intent.load_profile(m3_entities.DEFAULT_PROFILE)
    assert prof is not None, "the shipped entity profile did not load"
    return dataclasses.replace(prof, url="http://127.0.0.1:1234/v1/messages", backend="anthropic")


def test_a_local_profile_runs_keyless_on_the_configured_llm(monkeypatch):
    monkeypatch.setattr(
        m3_entities,
        "localize_endpoint",
        lambda url, backend: ("http://10.0.0.5:11434/v1/chat/completions", "openai"),
    )
    monkeypatch.setattr("auth_utils.get_api_key", lambda name: None)
    eff = m3_entities.effective_profile(_local_anthropic_profile())
    assert eff.url == "http://10.0.0.5:11434/v1/chat/completions" and eff.backend == "openai"
    assert slm_intent.job_api_key(eff) == "", "a keyless OpenAI-format call must not need a key"


def test_an_anthropic_endpoint_still_requires_its_key(monkeypatch):
    """A profile that really calls an Anthropic-format API keeps the rule."""
    monkeypatch.setattr(m3_entities, "localize_endpoint", lambda url, backend: (url, backend))
    monkeypatch.setattr("auth_utils.get_api_key", lambda name: None)
    prof = dataclasses.replace(_local_anthropic_profile(), url="https://api.anthropic.com/v1/messages")
    with pytest.raises(SystemExit, match="API key not found"):
        slm_intent.job_api_key(m3_entities.effective_profile(prof))


def test_a_run_without_store_flags_reads_the_configured_stores():
    """The defaults named <repo>/memory/*.db, the layout before the decoupled
    roots, so `m3_entities.py --dry-run` found no store on a real install."""
    import chatlog_config
    from m3_core.paths import resolve_db_path

    legacy = m3_entities.REPO_ROOT / "memory"
    assert m3_entities._default_core_db() == m3_entities.Path(resolve_db_path(None))
    assert m3_entities._default_chatlog_db() == m3_entities.Path(chatlog_config.chatlog_db_path())
    assert legacy not in m3_entities._default_core_db().parents
    assert legacy not in m3_entities._default_chatlog_db().parents
