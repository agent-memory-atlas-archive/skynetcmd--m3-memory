"""Finding the LLM a machine already uses, for memory enrichment: setup offers it,
doctor reports it. Each signal and each probe outcome is planted and must give
the documented answer; nothing here reaches a real server."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import httpx
import pytest

from m3_memory.install import llm_detect as D
from m3_memory.install.env_sources import EnvFound


def _env(**vals):
    return [EnvFound(k, v, "environment", "process") for k, v in vals.items()]


@pytest.mark.parametrize(
    "host, url",
    [
        ("", "http://localhost:11434/v1"),
        ("0.0.0.0", "http://localhost:11434/v1"),  # a bind address means this machine
        ("0.0.0.0:11500", "http://localhost:11500/v1"),
        ("gpu-box", "http://gpu-box:11434/v1"),
        ("172.20.0.5:11434", "http://172.20.0.5:11434/v1"),
        ("https://ollama.com", "https://ollama.com:443/v1"),
    ],
)
def test_ollama_host_becomes_a_base_url(host, url):
    assert D.ollama_url(host) == url


def test_a_remote_ollama_host_is_a_candidate_without_the_cloud_key():
    c = D.find_candidates(_env(OLLAMA_HOST="172.20.0.5", OLLAMA_API_KEY="cloud-key"))
    remote = next(x for x in c if x.url == "http://172.20.0.5:11434/v1")
    assert remote.runtime == "Ollama" and remote.key is None, "the ollama.com key never goes to a self-hosted server"


def test_the_ollama_cloud_key_goes_only_to_ollama_com():
    c = D.find_candidates(_env(OLLAMA_HOST="https://ollama.com", OLLAMA_API_KEY="cloud-key"))
    cloud = next(x for x in c if "ollama.com" in x.url)
    assert cloud.key == "cloud-key" and cloud.key_name == "OLLAMA_API_KEY"


def test_an_openai_base_url_brings_its_key_and_lm_studio_its_token():
    c = {
        x.url: x
        for x in D.find_candidates(_env(OPENAI_BASE_URL="http://h:8000/v1/", OPENAI_API_KEY="k1", LM_API_TOKEN="k2"))
    }
    assert c["http://h:8000/v1"].key == "k1" and c["http://h:8000/v1"].runtime == "OpenAI-compatible"
    assert c[D.LOCAL_LMSTUDIO].key == "k2" and c[D.LOCAL_LMSTUDIO].key_name == "LM_API_TOKEN"
    assert c[D.LOCAL_OLLAMA].key is None


def test_the_default_ports_are_always_candidates_once():
    urls = [x.url for x in D.find_candidates(_env(OLLAMA_HOST="0.0.0.0"))]
    assert urls.count(D.LOCAL_OLLAMA) == 1 and D.LOCAL_LMSTUDIO in urls


def test_another_tools_mcp_entry_is_not_a_signal():
    other = [EnvFound("OLLAMA_HOST", "172.20.9.9", "cfg.json", "mcp", server="x", m3_server=False)]
    assert all("172.20.9.9" not in x.url for x in D.find_candidates(other))


def _resp(status, models=()):
    return SimpleNamespace(status_code=status, json=lambda: {"data": [{"id": m} for m in models]})


def test_probe_outcomes(monkeypatch):
    calls = []

    def fake(url, headers, timeout):
        calls.append(headers)
        mode = fake.mode
        if mode == "open":
            return _resp(200, ["m1"])
        if mode == "auth":
            return _resp(200, ["m2"]) if headers.get("Authorization") == "Bearer good" else _resp(401)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", fake)
    c = D.Candidate("LM Studio", D.LOCAL_LMSTUDIO, "t", "LM_API_TOKEN", "good")
    fake.mode = "open"
    p = D.probe(c)
    assert p.state == "ok" and p.models == ["m1"] and calls[-1] == {}, "a keyless server is asked without a key"
    fake.mode = "auth"
    assert D.probe(c).state == "works-with-key"
    assert D.probe(D.Candidate("LM Studio", D.LOCAL_LMSTUDIO, "t")).state == "needs-key"
    fake.mode = "down"
    assert D.probe(c).state == "unreachable"


def test_local_and_cloud_hosts():
    assert D.is_cloud("http://localhost:11434/v1") is False
    assert D.is_cloud("http://172.20.0.9:11434/v1") is False
    assert D.is_cloud("http://100.64.0.7:11434/v1") is False  # tailnet
    assert D.is_cloud("http://8.8.8.8/v1") is True
    assert D.is_cloud("http://no-such-host.invalid/v1") is None


# ── the doctor/setup section ────────────────────────────────────────────────


@pytest.fixture
def llm_state(monkeypatch):
    import llm_failover
    from m3_core.config import settings as st

    state = SimpleNamespace(endpoints=[], explicit=False, probes={}, found=[])
    monkeypatch.setattr(llm_failover, "endpoints_from_settings", lambda: list(state.endpoints))
    monkeypatch.setattr(
        st,
        "setting",
        lambda k: (state.endpoints if state.explicit else None) if k in ("llm.endpoints", "llm.url") else None,
    )
    monkeypatch.setattr(D, "probe", lambda c, timeout=2.0: D.Probe(c, state.probes.get(c.url, "unreachable"), ["m"]))
    monkeypatch.setattr(D, "find_candidates", lambda found=None: list(state.found))
    return state


def test_no_llm_is_an_advisory_naming_the_setting_and_what_was_found(llm_state, capsys):
    from m3_memory.install import sections

    llm_state.found = [D.Candidate("Ollama", "http://172.20.0.5:11434/v1", "OLLAMA_HOST in environment")]
    llm_state.probes = {"http://172.20.0.5:11434/v1": "ok"}
    assert sections.llm_enrichment_section(brief=True) == 1
    out = capsys.readouterr().out
    assert "no LLM set up" in out and "m3 config set llm.endpoints <url>" in out
    assert "m3 config set llm.endpoints http://172.20.0.5:11434/v1" in out


def test_a_set_endpoint_that_wants_a_key_names_the_secret(llm_state, capsys):
    from m3_memory.install import sections

    llm_state.endpoints, llm_state.explicit = ["http://h:1234/v1"], True
    llm_state.probes = {"http://h:1234/v1": "needs-key"}
    assert sections.llm_enrichment_section() == 1
    assert "m3 secret set LM_API_TOKEN" in capsys.readouterr().out


def test_a_working_endpoint_is_one_ok_line(llm_state, capsys):
    from m3_memory.install import sections

    llm_state.endpoints, llm_state.explicit = ["http://h:11434/v1"], True
    llm_state.probes = {"http://h:11434/v1": "ok"}
    assert sections.llm_enrichment_section(brief=True) == 0
    assert "✅ memory enrichment: http://h:11434/v1" in capsys.readouterr().out


# ── setup ───────────────────────────────────────────────────────────────────


def _offer(monkeypatch, state):
    from m3_memory import setup_wizard as w

    cand = D.Candidate("LM Studio", D.LOCAL_LMSTUDIO, "t", "LM_API_TOKEN", "the-key")
    monkeypatch.setattr(D, "find_candidates", lambda found=None: [cand])
    monkeypatch.setattr(D, "probe", lambda c, timeout=2.0: D.Probe(c, state, ["m"]))
    monkeypatch.setattr(D, "is_cloud", lambda url: False)
    recorded, stored = [], []
    monkeypatch.setattr(w, "_record_llm_switch", lambda var, val: recorded.append((var, val)))
    import m3_core.secret_store as ss

    monkeypatch.setattr(ss, "set_secret", lambda n, v: stored.append((n, v)) or "secrets-file")
    return w, recorded, stored


def test_interactive_setup_writes_the_choice_and_stores_the_key_that_worked(monkeypatch):
    w, recorded, stored = _offer(monkeypatch, "works-with-key")
    monkeypatch.setattr(w, "_called_by_upgrade", lambda: False)
    monkeypatch.setattr(w, "_ask_choice", lambda q, choices, default: "1")
    monkeypatch.setattr(w, "_ask_yes_no", lambda q, default: True)
    assert w._offer_detected_llm(argparse.Namespace(non_interactive=False)) is True
    assert recorded == [("M3_LLM_ENDPOINTS_CSV", D.LOCAL_LMSTUDIO)]
    assert stored == [("LM_API_TOKEN", "the-key")]


def test_an_upgrade_reports_and_writes_nothing(monkeypatch, capsys):
    w, recorded, stored = _offer(monkeypatch, "ok")
    monkeypatch.setattr(w, "_called_by_upgrade", lambda: True)
    assert w._offer_detected_llm(argparse.Namespace(non_interactive=True)) is False
    assert recorded == [] and stored == []
    assert "m3 config set llm.endpoints <url>" in capsys.readouterr().out
