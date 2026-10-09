"""retries < 1 used to skip the request loop and return None, so the caller's
`.json()` failed far from the cause. It is refused up front now."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from m3_core.context import M3Context


@pytest.mark.parametrize("retries", [0, -1])
def test_no_attempts_is_refused(retries):
    fake = SimpleNamespace(_check_circuit=lambda service: True,
                           get_async_client=lambda: SimpleNamespace())
    with pytest.raises(ValueError, match=r"retries must be >= 1; observed: -?\d"):
        asyncio.run(M3Context.request_with_retry(fake, "GET", "http://h/x", retries=retries))


def test_an_open_circuit_raises_a_readable_http_error():
    """The circuit-open error carries a real response (503), so a handler that
    reads e.response.status_code works as for any other HTTP failure."""
    import httpx

    fake = SimpleNamespace(_check_circuit=lambda service: False,
                           get_async_client=lambda: SimpleNamespace())
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(M3Context.request_with_retry(fake, "GET", "http://h/x"))
    assert ei.value.response.status_code == 503
    assert ei.value.request.url == "http://h/x"
