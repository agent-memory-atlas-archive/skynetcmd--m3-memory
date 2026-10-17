"""The requires_llm skip says why the probe failed.

It used to read "no reachable local chat model" for every failure: a refused
token (LM Studio's 401), a crypto error while reading the token, and an empty
reply looked the same, so a skip was misread as "no model loaded".
"""

from __future__ import annotations

import httpx
import pytest

import conftest


class _Reply:
    def __init__(self, status: int, content: str = "ok") -> None:
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


@pytest.mark.parametrize(
    "outcome, expect",
    [
        (_Reply(401), "refused the request (HTTP 401)"),
        (_Reply(200, ""), "answered with an empty reply"),
        (httpx.ConnectError("refused"), "no chat model answered at"),
    ],
)
def test_each_failure_names_its_cause(monkeypatch, outcome, expect):
    def post(*a, **kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("M3_WIKI_DRIFT_URL", "http://127.0.0.1:9/v1/chat/completions")
    assert conftest._llm_chat_reachable() is False
    reason = conftest._llm_skip_reason()
    assert expect in reason and "127.0.0.1:9" in reason, reason


def test_a_working_endpoint_passes(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Reply(200, "ok"))
    monkeypatch.setenv("M3_WIKI_DRIFT_URL", "http://127.0.0.1:9/v1/chat/completions")
    assert conftest._llm_chat_reachable() is True
