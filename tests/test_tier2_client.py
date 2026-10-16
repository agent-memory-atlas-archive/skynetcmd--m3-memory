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


def test_a_secrets_fault_sends_no_token_instead_of_failing_the_embed(monkeypatch):
    import m3_sdk

    def boom(name):
        raise RuntimeError("secrets file refused")

    monkeypatch.setattr(m3_sdk, "get_secret_cached", boom)
    assert t.headers("https://embed.example.ts.net:8082") == {}


def test_only_http_urls_are_opened():
    with pytest.raises(ValueError, match="not an http"):
        t.urlopen("file:///etc/passwd", timeout=1)


def test_https_is_verified_with_m3s_context(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(t, "ssl_context", lambda: sentinel)
    assert t.verify_for("https://h:8082") is sentinel
    assert t.verify_for("http://127.0.0.1:8082") is True


def test_the_probe_reports_a_dead_port_with_its_cause():
    # Windows retries a refused SYN for about a second, so a short timeout may
    # observe the dead port as a timeout; both name the server and a fix.
    res = t.probe("http://127.0.0.1:9", timeout=0.5)
    assert res["status"] in ("refused", "timeout"), res
    assert "127.0.0.1:9" in res["error"] and res["fix"]


# ── the classifier: one Diagnosis per outcome ────────────────────────────────
URL = "http://127.0.0.1:8082"


def test_401_is_auth_rejected_never_down(monkeypatch):
    monkeypatch.setattr(t, "_token", lambda: None)
    d = t.classify(401, URL)
    assert d.state == "auth-rejected" and d.answering and not d.serving
    assert "m3 secret set M3_EMBED_SERVER_TOKEN" in d.fix
    assert "no M3_EMBED_SERVER_TOKEN is set" in d.fix


def test_403_with_a_token_says_the_token_was_rejected(monkeypatch):
    monkeypatch.setattr(t, "_token", lambda: "tok-123")
    d = t.classify(403, "https://embed.example.ts.net:8082")
    assert d.state == "auth-rejected" and "rejected this machine's" in d.fix
    assert "tok-123" not in d.message()


def test_a_token_withheld_over_plain_http_is_named(monkeypatch):
    monkeypatch.setattr(t, "_token", lambda: "tok-123")
    d = t.classify(401, "http://100.100.50.63:8082")
    assert "was not sent" in d.fix and "https://" in d.fix


def test_urllib_http_error_is_classified_by_its_status(monkeypatch):
    import io
    import urllib.error

    monkeypatch.setattr(t, "_token", lambda: None)
    e = urllib.error.HTTPError(URL, 401, "Unauthorized", None, io.BytesIO(b"no"))
    assert t.classify(e, URL).state == "auth-rejected"


def test_an_untrusted_certificate_names_the_ca_setting():
    import ssl
    import urllib.error

    cert = ssl.SSLCertVerificationError(1, "certificate verify failed")
    cert.verify_message = "unable to get local issuer certificate"
    d = t.classify(urllib.error.URLError(cert), "https://h.example:8082")
    assert d.state == "tls-untrusted" and d.answering
    assert "m3 config set embed.fallback_ca" in d.fix and "local issuer" in d.detail


def test_a_wrapped_certificate_failure_is_found_through_the_chain():
    """httpx raises ConnectError from the ssl error; the chain is walked."""
    import ssl

    class ConnectError(Exception):
        pass

    try:
        try:
            raise ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed")
        except ssl.SSLError as inner:
            raise ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]") from inner
    except ConnectError as outer:
        assert t.classify(outer, "https://h:8082").state == "tls-untrusted"


def test_a_bad_ca_file_is_named_on_a_certificate_failure(monkeypatch):
    import ssl

    monkeypatch.setattr(t, "_CA_FAILURE", "embed.fallback_ca '/nope.pem' could not be loaded (FileNotFoundError)")
    d = t.classify(ssl.SSLCertVerificationError(1, "verify failed"), "https://h:8082")
    assert "/nope.pem" in d.fix


@pytest.mark.parametrize(
    "url,expect", [(URL, "m3 embedder start"), ("https://gpu-box.example:8082", "not listening there")]
)
def test_refused_says_start_it_here_only_for_loopback(url, expect):
    d = t.classify(ConnectionRefusedError(111, "Connection refused"), url)
    assert d.state == "refused" and not d.answering and expect in d.fix


def test_windows_refusal_text_through_httpx_style_wrapper():
    class ConnectError(Exception):
        pass

    e = ConnectError("[WinError 10061] No connection could be made because the target machine actively refused it")
    assert t.classify(e, URL).state == "refused"


def test_timeout_is_its_own_state_with_the_budget():
    class ReadTimeout(Exception):
        pass

    d = t.classify(ReadTimeout(""), URL, timeout=30)
    assert d.state == "timeout" and "within 30s" in d.detail and "ReadTimeout" in d.detail


def test_degraded_503_shows_the_reason():
    d = t.classify(503, URL, '{"status":"degraded","reason":"circuit open after 5 failures"}')
    assert d.state == "unhealthy-503" and "circuit open after 5 failures" in d.detail


def test_non_http_scheme_is_bad_scheme_unread():
    d = t.classify(ValueError("x"), "file:///etc/passwd")
    assert d.state == "bad-scheme" and "embed.fallback_url" in d.fix


def test_body_snippets_are_short_and_redacted():
    body = "Authorization: Bearer abcdefghijklmnop " + "x" * 500
    d = t.classify(500, URL, body)
    assert "abcdefghijklmnop" not in d.detail and "[REDACTED]" in d.detail
    assert len(t.snippet(body)) <= 200


@pytest.mark.parametrize(
    "status,raw,state",
    [
        (200, "OK", "ok"),
        (200, '{"status":"ok","model":"bge-m3"}', "ok"),
        (200, '{"status":"loading"}', "loading"),
        (503, '{"status":"loading model"}', "loading"),
        (200, '{"status":"error"}', "unhealthy-200"),
        (200, "<html>gateway</html>", "bad-response"),
        (503, '{"status":"degraded","reason":"breaker open"}', "unhealthy-503"),
        (401, "", "auth-rejected"),
    ],
)
def test_health_contracts(monkeypatch, status, raw, state):
    monkeypatch.setattr(t, "_token", lambda: None)
    d, _body = t.classify_health(status, raw, URL)
    assert d.state == state, d


def test_probe_reports_the_classified_state_and_fix(monkeypatch):
    def refused(url, **kw):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(t, "urlopen", refused)
    res = t.probe(URL, timeout=1, source="default")
    assert res["status"] == "refused" and res["source"] == "default"
    assert "m3 embedder start" in res["fix"] and "127.0.0.1:8082" in res["error"]


# ── the URL resolver: one precedence ─────────────────────────────────────────
def test_a_set_fallback_url_wins_over_the_legacy_file(m3_settings):
    m3_settings({"embed.fallback_url": "https://set.example:8082/"})
    url, source = t.fallback_url({"fallback_url": "http://127.0.0.1:9999"})
    assert url == "https://set.example:8082" and source.startswith("embed.fallback_url")


def test_the_legacy_file_wins_over_the_default(m3_settings):
    m3_settings()
    url, source = t.fallback_url({"fallback_url": "http://127.0.0.1:9999/"})
    assert url == "http://127.0.0.1:9999" and ".embed_config.json" in source


def test_the_default_follows_the_configured_server_port(m3_settings):
    m3_settings({"embed.server.port": 8083})
    assert t.fallback_url({}) == ("http://127.0.0.1:8083", "default (this machine, embed.server.port)")


def test_the_resolver_reads_the_legacy_file_and_says_a_malformed_one(m3_settings, monkeypatch, tmp_path, caplog):
    m3_settings()
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    (tmp_path / ".embed_config.json").write_text('{"fallback_url": "http://127.0.0.1:7777"}', encoding="utf-8")
    assert t.fallback_url()[0] == "http://127.0.0.1:7777"
    (tmp_path / ".embed_config.json").write_text("{ not json", encoding="utf-8")
    t._SAID.clear()
    with caplog.at_level(logging.WARNING, logger="m3_core.tier2_client"):
        assert t.fallback_url()[1].startswith("default")
    assert any("unreadable" in r.getMessage() for r in caplog.records)


def test_a_secrets_fault_is_said_once_with_its_consequence(monkeypatch, caplog):
    import m3_sdk

    def boom(name):
        raise RuntimeError("secrets file refused")

    monkeypatch.setattr(m3_sdk, "get_secret_cached", boom)
    t._SAID.clear()
    with caplog.at_level(logging.WARNING, logger="m3_core.tier2_client"):
        assert t._token() is None
        assert t._token() is None
    said = [r.getMessage() for r in caplog.records if "cannot resolve secret" in r.getMessage()]
    assert len(said) == 1 and "401" in said[0]


def test_a_bad_ca_file_degrades_to_default_trust_and_is_said(monkeypatch, m3_settings, tmp_path, caplog):
    m3_settings({"embed.fallback_ca": str(tmp_path / "missing.pem")})
    t.ssl_context.cache_clear()
    monkeypatch.setattr(t, "_CA_FAILURE", None)
    try:
        with caplog.at_level(logging.ERROR, logger="m3_core.tier2_client"):
            ctx = t.ssl_context()
        assert ctx is not None
        assert t._CA_FAILURE and "missing.pem" in t._CA_FAILURE
        assert any("embed.fallback_ca" in r.getMessage() for r in caplog.records)
    finally:
        t.ssl_context.cache_clear()
