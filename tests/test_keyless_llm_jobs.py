"""LLM jobs run against a server that needs no key (Ollama, LM Studio without
auth). They refused to start without a stored key, so the cognitive loop stopped
on every keyless install (2026-10-18). Now a missing key is refused only where
the API always needs one (Anthropic format); otherwise the job calls without an
Authorization header and a 401 says what to set."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _profile(backend="openai", service="LM_API_TOKEN"):
    return SimpleNamespace(
        backend=backend, api_key_service=service, url="http://ollama.local:11434/v1/chat/completions"
    )


def test_a_keyless_openai_profile_runs_without_a_key(monkeypatch, capsys):
    import auth_utils
    import slm_intent

    monkeypatch.setattr(auth_utils, "get_api_key", lambda service: None)
    assert slm_intent.job_api_key(_profile()) == ""
    err = capsys.readouterr().err
    assert "calling http://ollama.local" in err and "without one" in err


def test_a_stored_key_is_used(monkeypatch):
    import auth_utils
    import slm_intent

    monkeypatch.setattr(auth_utils, "get_api_key", lambda service: "tok-123")
    assert slm_intent.job_api_key(_profile()) == "tok-123"


def test_an_anthropic_profile_without_a_key_still_stops(monkeypatch):
    import auth_utils
    import slm_intent

    monkeypatch.setattr(auth_utils, "get_api_key", lambda service: None)
    with pytest.raises(SystemExit, match="m3 secret set ANTHROPIC_API_KEY"):
        slm_intent.job_api_key(_profile(backend="anthropic", service="ANTHROPIC_API_KEY"))


def test_no_key_sends_no_authorization_header():
    from llm_failover import auth_headers

    assert auth_headers("") == {}
    assert auth_headers(None) == {}
    assert auth_headers("tok") == {"Authorization": "Bearer tok"}


def test_a_401_says_which_secret_to_set_and_other_errors_do_not():
    from llm_failover import unauthorized_hint

    assert "m3 secret set LM_API_TOKEN" in unauthorized_hint(401, "LM_API_TOKEN")
    assert "m3 secret set LM_API_TOKEN" in unauthorized_hint(403, "LM_API_TOKEN")
    assert unauthorized_hint(500, "LM_API_TOKEN") == ""
    assert unauthorized_hint(404, "LM_API_TOKEN") == ""
