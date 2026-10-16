"""Regression: tier-2 (_probe_tier2) must accept the shared in-process
embedder's JSON /health contract, not only the legacy Rust plaintext "OK".

Background: the system migrated from the legacy Rust m3-embed-server (which
replies `OK` to GET /health) to the shared in-process embedder
(embed_server_inproc.py), which replies JSON
`{"status": "ok", "model": ..., "dim": ...}`. _probe_tier2 originally required
`body == "OK"` and stamped the healthy shared server as `unhealthy-200`,
turning the whole cascade `broken` even though the server embeds fine. These
tests pin the two accepted contracts + the genuinely-unhealthy case so the
stale-contract bug cannot silently return.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory import doctor as _doctor  # noqa: E402


class _FakeResp:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _patch_conn(monkeypatch, responses: dict[str, _FakeResp]) -> None:
    """Script tier2_client.urlopen per path. A missing path is a 404, raised
    as urllib does; /metrics defaults to that (the shared server has none)."""
    import urllib.error
    import urllib.parse

    from m3_core import tier2_client

    def fake(url, *, timeout, data=None, extra_headers=None):
        path = urllib.parse.urlsplit(url).path
        if path not in responses:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return responses[path]

    monkeypatch.setattr(tier2_client, "urlopen", fake)


def test_tier2_accepts_shared_json_health(monkeypatch):
    """Shared in-process embedder: JSON body with status 'ok' -> online.
    This is the exact response that used to be misclassified unhealthy-200."""
    _patch_conn(
        monkeypatch,
        {
            "/health": _FakeResp(200, '{"status":"ok","model":"bge-m3-GGUF-Q4_K_M.gguf","dim":1024}'),
            # shared server has no /metrics — 404 is expected, must not fault
        },
    )
    res = _doctor._probe_tier2()
    assert res["status"] == "online", res
    # model is surfaced from the /health JSON body (no /metrics on shared server)
    assert res["model"] == "bge-m3-GGUF-Q4_K_M.gguf", res


def test_tier2_accepts_shared_json_loading(monkeypatch):
    """status 'loading' (model still warming) is also healthy, matching
    shared_embedder_probe.py's contract."""
    _patch_conn(
        monkeypatch,
        {
            "/health": _FakeResp(200, '{"status":"loading"}'),
        },
    )
    res = _doctor._probe_tier2()
    assert res["status"] == "online", res


def test_tier2_accepts_legacy_plaintext_ok(monkeypatch):
    """Backward compat: the legacy Rust m3-embed-server replies plaintext
    'OK' and still exposes /metrics — must remain online with model set."""
    _patch_conn(
        monkeypatch,
        {
            "/health": _FakeResp(200, "OK"),
            "/metrics": _FakeResp(200, '{"model":"legacy-rust-bge"}'),
        },
    )
    res = _doctor._probe_tier2()
    assert res["status"] == "online", res
    assert res["model"] == "legacy-rust-bge", res


def test_tier2_rejects_non_healthy_json(monkeypatch):
    """A 200 whose JSON says the server is NOT ready must still be
    unhealthy — the fix widened the contract, it must not blanket-accept 200."""
    _patch_conn(
        monkeypatch,
        {
            "/health": _FakeResp(200, '{"status":"error"}'),
        },
    )
    res = _doctor._probe_tier2()
    assert res["status"] == "unhealthy-200", res


def test_tier2_rejects_unparseable_200_body(monkeypatch):
    """A 200 with a non-JSON, non-'OK' body is unhealthy, not online."""
    _patch_conn(
        monkeypatch,
        {
            "/health": _FakeResp(200, "<html>gateway</html>"),
        },
    )
    res = _doctor._probe_tier2()
    # Not the health contract at all: something else answers the port.
    assert res["status"] == "bad-response", res
    assert res["fix"] and "embed.fallback_url" in res["fix"], res


def test_tier2_401_is_auth_rejected_with_the_token_fix(monkeypatch):
    """A token-protected server answering 401 is reachable and misconfigured,
    never "offline" with no error (the old probe said unhealthy-401, error=None)."""
    import io
    import urllib.error

    from m3_core import tier2_client

    def fake(url, *, timeout, data=None, extra_headers=None):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, io.BytesIO(b""))

    monkeypatch.setattr(tier2_client, "urlopen", fake)
    monkeypatch.setattr(tier2_client, "_token", lambda: None)
    res = _doctor._probe_tier2()
    assert res["status"] == "auth-rejected", res
    assert "m3 secret set M3_EMBED_SERVER_TOKEN" in res["fix"]
    assert res["source"], "doctor must say where the URL came from"
