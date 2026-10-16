"""Regression: shared_embedder_probe._server_health must accept BOTH health
contracts — the legacy Rust m3-embed-server's plaintext "OK" AND the Python
embed_server_inproc.py's JSON {"status": ...}.

Background: _server_health json.loads'd the /health body unconditionally, so the
Rust server's plaintext "OK" raised JSONDecodeError → caught by the bare except →
returned "down". `m3 doctor` then false-reported a healthy :8082 tier-2 embedder
as [FAIL] on every install running the Rust embedder (the shipped default). These
tests pin both accepted contracts + the genuinely-down / malformed cases so the
stale-contract bug cannot silently return.

Hermetic: tier2_client.urlopen is mocked (no live server, CI-safe). The logic
is pure HTTP/JSON with no OS branch, so one test covers all three supported OSes.
"""

from __future__ import annotations

import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from doctor import shared_embedder_probe as P  # noqa: E402


class _FakeResp:
    def __init__(self, body: str) -> None:
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body


def _mock_urlopen(monkeypatch, body: str) -> None:
    from m3_core import tier2_client

    @contextlib.contextmanager
    def _fake(url, *, timeout, data=None, extra_headers=None):  # noqa: ARG001 — signature parity
        yield _FakeResp(body)

    monkeypatch.setattr(tier2_client, "urlopen", _fake)


def test_plaintext_ok_is_healthy(monkeypatch):
    """The Rust m3-embed-server replies plaintext 'OK' — must be state 'ok'
    (this is the exact case the old json.loads-first code false-reported down)."""
    _mock_urlopen(monkeypatch, "OK")
    diag, _body = P._server_health("http://127.0.0.1:8082")
    assert diag.state == "ok", f"plaintext OK must be healthy, got {diag!r}"


def test_json_ok_is_healthy(monkeypatch):
    """The Python embed_server_inproc.py replies JSON {'status':'ok',...}."""
    _mock_urlopen(monkeypatch, '{"status": "ok", "model": "bge-m3", "dim": 1024}')
    diag, body = P._server_health("http://127.0.0.1:8082")
    assert diag.state == "ok"
    assert body.get("model") == "bge-m3"


def test_json_loading_is_reported_loading(monkeypatch):
    """A warming server replies {'status':'loading'} — surfaced, not treated ok."""
    _mock_urlopen(monkeypatch, '{"status": "loading"}')
    diag, _ = P._server_health("http://127.0.0.1:8082")
    assert diag.state == "loading"


def test_unknown_json_status_is_unhealthy(monkeypatch):
    _mock_urlopen(monkeypatch, '{"status": "boom"}')
    diag, _ = P._server_health("http://127.0.0.1:8082")
    assert diag.state == "unhealthy-200" and "'boom'" in diag.detail


def test_non_ok_non_json_is_a_bad_response(monkeypatch):
    """A body that is neither 'OK' nor valid JSON is not the health contract
    (something else may hold the port); never raises (§3)."""
    _mock_urlopen(monkeypatch, "Internal Server Error")
    diag, _ = P._server_health("http://127.0.0.1:8082")
    assert diag.state == "bad-response" and not diag.serving


def test_bad_scheme_is_rejected():
    """A non-http(s) URL is rejected before any network call (nosec B310 guard)."""
    diag, _ = P._server_health("file:///etc/passwd")
    assert diag.state == "bad-scheme"


def test_connection_error_is_refused(monkeypatch):
    """urlopen raising connection refused → 'refused' with the start fix, never propagates."""

    from m3_core import tier2_client

    def _boom(url, *, timeout, data=None, extra_headers=None):  # noqa: ARG001
        raise OSError("connection refused")

    monkeypatch.setattr(tier2_client, "urlopen", _boom)
    diag, _ = P._server_health("http://127.0.0.1:8082")
    assert diag.state == "refused" and "m3 embedder start" in diag.fix
