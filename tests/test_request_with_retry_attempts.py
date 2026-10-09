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
