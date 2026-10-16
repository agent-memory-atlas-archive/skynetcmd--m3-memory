"""HTTP faults behind best-effort fallbacks are reported, not hidden.

* m3_core.errors.log_http_fault: an LLM endpoint answering 401/500 on every
  call used to land at DEBUG (log_swallowed files HTTPStatusError as an
  expected miss). Now: one WARNING per (URL, status), the URL without its
  query, then DEBUG.
* m3_core.doorbell: a daemon that ANSWERS with an error is not an absent daemon.
* wiki judges: fail-open stays, but the run summary names the top reason.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from m3_core import errors


def _status_exc(status: int, url: str) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", url)
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(status, request=req))


@pytest.fixture(autouse=True)
def _fresh_warned(monkeypatch):
    monkeypatch.setattr(errors, "_http_fault_warned", set())


def test_an_http_fault_warns_once_per_url_and_status(caplog):
    log = logging.getLogger("t.http_fault")
    exc = _status_exc(401, "https://llm.example/v1/chat/completions?key=SECRET")
    with caplog.at_level(logging.DEBUG, logger="t.http_fault"):
        errors.log_http_fault(log, exc, "auto-title", "keeping the original title")
        errors.log_http_fault(log, exc, "auto-title", "keeping the original title")
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    text = warns[0].getMessage()
    assert "HTTP 401" in text and "https://llm.example/v1/chat/completions" in text
    assert "keeping the original title" in text
    assert "SECRET" not in caplog.text


def test_a_different_status_warns_again(caplog):
    log = logging.getLogger("t.http_fault2")
    with caplog.at_level(logging.WARNING, logger="t.http_fault2"):
        errors.log_http_fault(log, _status_exc(401, "http://h/v1"), "x", "fb")
        errors.log_http_fault(log, _status_exc(500, "http://h/v1"), "x", "fb")
    assert len(caplog.records) == 2


def test_a_non_http_error_keeps_log_swallowed_behaviour(caplog):
    log = logging.getLogger("t.http_fault3")
    with caplog.at_level(logging.DEBUG, logger="t.http_fault3"):
        errors.log_http_fault(log, KeyError("choices"), "x", "fb")
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]


def test_describe_http_failure_never_carries_the_query():
    out = errors.describe_http_failure(_status_exc(403, "https://h/v1/x?key=SECRET"), "POST", body_chars=50)
    assert out.startswith("POST https://h/v1/x -> HTTP 403")
    assert "SECRET" not in out


def test_a_body_echoing_the_bearer_is_redacted():
    req = httpx.Request("POST", "https://h/v1/x")
    resp = httpx.Response(401, request=req, text="bad header: Authorization: Bearer sk-live-abcdef123456")
    out = errors.describe_http_failure(httpx.HTTPStatusError("x", request=req, response=resp), "POST", 200)
    assert "sk-live-abcdef123456" not in out and "Bearer [REDACTED]" in out


def test_redacted_snippet_keeps_a_placeholder_and_bounds_length():
    from m3_core.secret_store import redacted_snippet

    assert redacted_snippet("Bearer $LM_API_TOKEN") == "Bearer $LM_API_TOKEN"
    assert len(redacted_snippet("x" * 500, 50)) == 50


def test_loggable_url_strips_userinfo_and_query():
    from m3_core.secret_store import loggable_url

    assert loggable_url("https://u:pw@h:8443/v1/models?key=SECRET#f") == "https://h:8443/v1/models"
    assert loggable_url("not a url ?key=SECRET") == "not a url "


# ── doorbell ──────────────────────────────────────────────────────────────────


def test_doorbell_wait_on_a_daemon_error_returns_none_and_warns(monkeypatch, caplog):
    from m3_core import doorbell

    monkeypatch.setattr(doorbell, "_warned_at", {})
    monkeypatch.setattr(doorbell, "_request", lambda *a, **k: (500, {"error": "boom"}))
    with caplog.at_level(logging.WARNING, logger="m3.doorbell"):
        assert doorbell.wait("ep", ["agent"], 0.1) is None
        assert doorbell.wait("ep", ["agent"], 0.1) is None  # rate-limited: still one line
    assert len(caplog.records) == 1
    assert "HTTP 500" in caplog.text and "waiter polls" in caplog.text


def test_doorbell_absent_daemon_stays_silent(monkeypatch, caplog):
    from m3_core import doorbell

    def _refused(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(doorbell, "_request", _refused)
    with caplog.at_level(logging.WARNING, logger="m3.doorbell"):
        assert doorbell.wait("ep", ["agent"], 0.1) is None
    assert caplog.records == []


# ── wiki fail-open tallies ────────────────────────────────────────────────────


def test_call_failures_suffix_names_the_top_reason():
    from wiki.synth import CallFailures, call_failure_reason

    f = CallFailures()
    assert f.suffix() == ""
    for _ in range(3):
        f.record(call_failure_reason(_status_exc(401, "http://h/v1/chat?key=SECRET"), "http://h/v1/chat?key=SECRET"))
    f.record("empty reply")
    s = f.suffix()
    assert "4 model call(s) failed" in s and "(3x): HTTP 401 from http://h/v1/chat" in s
    assert "SECRET" not in s
