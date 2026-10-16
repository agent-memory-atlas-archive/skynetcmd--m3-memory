"""retries < 1 used to skip the request loop and return None, so the caller's
`.json()` failed far from the cause. It is refused up front now."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from m3_core.context import M3Context


@pytest.mark.parametrize("retries", [0, -1])
def test_no_attempts_is_refused(retries):
    fake = SimpleNamespace(_check_circuit=lambda service: True, get_async_client=lambda: SimpleNamespace())
    with pytest.raises(ValueError, match=r"retries must be >= 1; observed: -?\d"):
        asyncio.run(M3Context.request_with_retry(fake, "GET", "http://h/x", retries=retries))


def test_an_open_circuit_raises_a_readable_http_error(monkeypatch):
    """The circuit-open error carries a real response (503), so a handler that
    reads e.response.status_code works as for any other HTTP failure -- and is a
    distinct CircuitOpenError naming the last cause, so it is not mistaken for
    the server answering 503."""
    import httpx
    from m3_core import context as C

    monkeypatch.setitem(C._CIRCUIT_INFO, "h", {"last_error": "GET http://h/x -> HTTP 500: kaput", "open_until": 0.0})
    fake = SimpleNamespace(_check_circuit=lambda service: False, get_async_client=lambda: SimpleNamespace())
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(M3Context.request_with_retry(fake, "GET", "http://h/x?key=SECRET"))
    assert isinstance(ei.value, C.CircuitOpenError)
    assert ei.value.response.status_code == 503
    assert ei.value.response.headers["X-M3-Circuit"] == "open"
    assert "kaput" in str(ei.value) and "NOT sent" in str(ei.value)
    assert "SECRET" not in str(ei.value)


class _Client:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    async def request(self, method, url, **kw):
        import httpx

        self.calls += 1
        return httpx.Response(self.status, request=httpx.Request(method, url), text="bad input")


def _fake(client, failures):
    return SimpleNamespace(
        _check_circuit=lambda service: True,
        get_async_client=lambda: client,
        _record_success=lambda service: None,
        _record_failure=lambda service: failures.append(service),
    )


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_a_client_error_is_not_retried_and_does_not_trip_the_breaker(status):
    import httpx

    client, failures = _Client(status), []
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(M3Context.request_with_retry(_fake(client, failures), "POST", "http://h/x", retries=3))
    assert client.calls == 1
    assert failures == []


def test_a_server_error_is_retried_and_counted(monkeypatch):
    import httpx

    async def _no_sleep(_s):
        return None

    from m3_core import context as C

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(C, "_CIRCUIT_INFO", {})
    client, failures = _Client(503), []
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(M3Context.request_with_retry(_fake(client, failures), "GET", "http://h/x", retries=2))
    assert client.calls == 2
    assert failures == ["h"]
