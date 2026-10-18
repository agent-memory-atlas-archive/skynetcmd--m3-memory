"""fast_embedder_available() must not claim a breaker's half-open probe.

The breaker admits ONE probe after its cooldown (allow_request() returns True
once, then False until an outcome is recorded). The predictor used to call
allow_request(), so it took the probe, the real tier-2 call was refused, nothing
recorded an outcome, and tier 2 never came back. The breaker is a recorder with
the native core's semantics, so this runs with or without the core.
"""

from __future__ import annotations

import importlib


class _HalfOpenBreaker:
    """Cooldown elapsed: the first allow_request() is the probe."""

    def __init__(self):
        self.probe_taken = False

    def state(self):
        return "half_open"

    def allow_request(self):
        if self.probe_taken:
            return False
        self.probe_taken = True
        return True


class _OpenBreaker:
    def state(self):
        return "open"


def _predictor(monkeypatch, breaker, url):
    embed = importlib.import_module("memory.embed")
    monkeypatch.setattr(embed, "_CPU_FALLBACK_BREAKER", breaker)
    monkeypatch.setattr(embed, "_get_embedded_embedder", lambda: None)
    monkeypatch.setattr(embed, "_EMBED_FALLBACK_URL", url)
    monkeypatch.setattr(embed.config, "EMBED_FALLBACK_URL", url, raising=False)
    return embed.fast_embedder_available()


def test_no_breaker_means_try_the_embed_server(monkeypatch):
    """No breaker (the Rust core is not loaded) is the pre-breaker behaviour:
    try the server. It used to read as "unavailable", so a pure-Python install
    deferred every vector and dropped every query vector with a healthy server
    (found 2026-10-18: a second m3 without the core was first on PATH)."""
    assert _predictor(monkeypatch, None, "http://127.0.0.1:8082") is True


def test_an_open_breaker_or_no_server_skips_tier_2(monkeypatch):
    assert _predictor(monkeypatch, _OpenBreaker(), "http://127.0.0.1:8082") is False
    assert _predictor(monkeypatch, None, "") is False


def test_predictor_leaves_the_probe_for_the_real_call(monkeypatch):
    embed = importlib.import_module("memory.embed")
    breaker = _HalfOpenBreaker()
    monkeypatch.setattr(embed, "_CPU_FALLBACK_BREAKER", breaker)
    monkeypatch.setattr(embed, "_get_embedded_embedder", lambda: None)
    monkeypatch.setattr(embed, "_EMBED_FALLBACK_URL", "http://127.0.0.1:8082")

    assert embed.fast_embedder_available() is True
    assert breaker.allow_request() is True, "the predictor claimed the half-open probe"


def test_predictor_reports_an_open_breaker(monkeypatch):
    embed = importlib.import_module("memory.embed")

    class _Open(_HalfOpenBreaker):
        def state(self):
            return "open"

    monkeypatch.setattr(embed, "_CPU_FALLBACK_BREAKER", _Open())
    monkeypatch.setattr(embed, "_get_embedded_embedder", lambda: None)
    assert embed.fast_embedder_available() is False
