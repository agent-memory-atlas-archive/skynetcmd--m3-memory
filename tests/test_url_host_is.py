"""A provider is recognised by the URL's host, never by a substring: the answer
decides which header an API key is sent in (CodeQL
py/incomplete-url-substring-sanitization)."""

from __future__ import annotations

import pytest
from m3_core.urls import host_is


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://api.anthropic.com/v1", True),
        ("https://anthropic.com", True),
        ("https://API.Anthropic.COM./v1", True),
        ("https://other.example/anthropic.com", False),
        ("https://notanthropic.com", False),
        ("https://api.anthropic.com.evil.example", False),
        ("not a url", False),
        ("http://[::1", False),
    ],
)
def test_host_is(url, expected):
    assert host_is(url, "anthropic.com") is expected
