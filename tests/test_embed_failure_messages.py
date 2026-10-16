"""The embed cascade's failure lines carry cause, endpoint and fix, never the
memory text or a token; and a tripped primary breaker no longer skips tier 4."""

from __future__ import annotations

import importlib
import logging
from unittest.mock import MagicMock

import httpx
import pytest


def _emb():
    return importlib.import_module("memory.embed")


def _status_error(code: int, body: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://127.0.0.1:1234/v1/embeddings")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req, text=body))


# ── 413: a too-large BODY is not an over-long INPUT ──────────────────────────
def test_body_too_large_is_not_a_context_overflow():
    emb = _emb()
    body = '{"error":{"code":"body_too_large","message":"request body too large (input too long for the limit)"}}'
    assert emb._parse_context_overflow(413, body) is None
    assert emb._parse_context_overflow(413, "payload rejected (code=body_too_large): too many tokens") is None


def test_structured_input_too_long_is_a_context_overflow():
    emb = _emb()
    body = '{"error":{"code":"input_too_long","message":"9000 tokens > n_ctx 8192","observed_tokens":9000}}'
    over = emb._parse_context_overflow(413, body)
    assert over is not None and over.observed_tokens == 9000


def test_rust_plaintext_input_too_long_is_still_recognised():
    emb = _emb()
    text = "input too long: 9000 tokens > n_ctx 8192 (code=input_too_long observed_tokens=9000 max_tokens=8192)"
    assert emb._parse_context_overflow(413, text) is not None


# ── the length-contract log never carries the text ───────────────────────────
def test_contract_violation_log_identifies_without_the_text(caplog):
    emb = _emb()
    secret = "PRIVATE-MEMORY-CONTENT " * 50
    with caplog.at_level(logging.ERROR, logger=emb.logger.name):
        emb._log_contract_violation([secret], [(0, 99999)])
    msg = caplog.records[-1].getMessage()
    assert "PRIVATE-MEMORY-CONTENT" not in msg
    assert "content_hash" in msg and "attempting subdivision" in msg and "no row is lost" not in msg


def test_an_unrecovered_oversize_row_is_logged_as_lost(caplog):
    emb = _emb()
    with caplog.at_level(logging.ERROR, logger=emb.logger.name):
        emb._log_oversize_loss("PRIVATE " * 10, "bulk row 3 of 7")
    msg = caplog.records[-1].getMessage()
    assert "bulk row 3 of 7" in msg and "without a vector" in msg and "PRIVATE" not in msg


# ── tier 3 auth: no placeholder bearer; a 401 names the secret ───────────────
def test_no_lm_token_sends_no_authorization_header(monkeypatch):
    emb = _emb()

    class _Ctx:
        def get_secret(self, name):
            return None

    monkeypatch.setattr(emb, "_ctx", lambda: _Ctx())
    assert emb._lm_token() is None
    assert emb._auth_headers(emb._lm_token()) == {}


def test_a_401_names_the_secret_and_whether_a_token_went():
    emb = _emb()
    line = emb._http_failure(
        _status_error(401, "Unauthorized"), "http://127.0.0.1:1234/v1", auth_setting="LM_API_TOKEN", token_sent=False
    )
    assert "HTTP 401" in line and "m3 secret set LM_API_TOKEN" in line and "not sent" in line


def test_an_empty_timeout_message_still_says_what_happened():
    emb = _emb()
    line = emb._http_failure(httpx.ReadTimeout(""), "http://127.0.0.1:8082")
    assert "ReadTimeout" in line and "127.0.0.1:8082" in line


@pytest.mark.parametrize(
    "code,retry", [(400, False), (401, False), (404, False), (408, True), (429, True), (503, True)]
)
def test_only_retryable_statuses_are_retried(code, retry):
    assert _emb()._retryable_status(_status_error(code)) is retry


# ── a tripped tier-3 breaker skips tier 3, not tier 4 ────────────────────────
async def test_primary_breaker_open_still_tries_the_cloud(monkeypatch, caplog):
    emb = _emb()
    config = emb.config
    monkeypatch.setattr(config, "M3_ALLOW_CLOUD_FALLBACK", True)
    monkeypatch.setattr(config, "M3_CLOUD_ENCLAVE_URL", "http://enclave.test/embeddings")
    monkeypatch.setattr(emb, "_get_embedded_embedder", lambda: None)
    monkeypatch.setattr(emb, "_cloud_auth_token", lambda: None)

    class _Open:
        def allow_request(self):
            return False

        def record_failure(self):
            pass

    for name in ("_EMBEDDED_BREAKER", "_CLOUD_BREAKER"):
        monkeypatch.setattr(emb, name, None)
    monkeypatch.setattr(emb, "_CPU_FALLBACK_BREAKER", _Open())
    monkeypatch.setattr(emb, "_PRIMARY_BREAKER", _Open())
    monkeypatch.setattr(emb, "_LAST_SAID", {})

    proper = [1.0] + [0.0] * (config.EMBED_DIM - 1)
    posted: list[str] = []

    async def _post(self, url, json=None, headers=None, **kw):
        posted.append(str(url))
        resp = MagicMock()
        resp.json.return_value = {"data": [{"embedding": proper}]}
        resp.raise_for_status = lambda: None
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    monkeypatch.setattr(emb, "_EMBED_CLIENT", None, raising=False)
    with caplog.at_level(logging.WARNING, logger=emb.logger.name):
        vec, _model = await emb._embed("an uncached query for the breaker test")
    assert vec == proper and posted and all("enclave.test" in u for u in posted)
    assert any("tier 4 (cloud) is tried next" in r.getMessage() for r in caplog.records)
