"""The tier-2 client rule: the bearer token goes over https or to loopback,
never in plaintext across a network; only http(s) URLs are opened."""

from __future__ import annotations

import logging
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from m3_core import tier2_client as t  # noqa: E402


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(t, "_token", lambda: "tok-123")
    t._plaintext_warned.cache_clear()
    yield
    t._plaintext_warned.cache_clear()


@pytest.mark.parametrize(
    "url",
    [
        "https://embed.example.ts.net:8082/embedding",
        "https://100.100.50.63:8082/embedding",
        "http://127.0.0.1:8082/embedding",
        "http://[::1]:8082/embedding",
        "http://localhost:8082/embedding",
    ],
)
def test_the_token_goes_over_https_or_to_loopback(token, url):
    assert t.headers(url) == {"Authorization": "Bearer tok-123"}


def test_no_token_crosses_a_network_in_plaintext_and_that_is_said_once(token, caplog):
    with caplog.at_level(logging.WARNING, logger="m3_core.tier2_client"):
        assert t.headers("http://100.100.50.63:8082/embedding") == {}
        assert t.headers("http://100.100.50.63:8082/health") == {}
    warned = [r for r in caplog.records if "not sent" in r.getMessage()]
    assert len(warned) == 1 and "https://" in warned[0].getMessage()


def test_no_token_set_sends_nothing(monkeypatch):
    monkeypatch.setattr(t, "_token", lambda: None)
    assert t.headers("https://embed.example.ts.net:8082") == {}


def test_only_http_urls_are_opened():
    with pytest.raises(ValueError, match="not an http"):
        t.urlopen("file:///etc/passwd", timeout=1)


def test_https_is_verified_with_m3s_context(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(t, "ssl_context", lambda: sentinel)
    assert t.verify_for("https://h:8082") is sentinel
    assert t.verify_for("http://127.0.0.1:8082") is True


def test_the_probe_reports_a_dead_port_offline():
    res = t.probe("http://127.0.0.1:9", timeout=0.5)
    assert res["status"] == "offline" and res["error"]
