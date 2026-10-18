"""URL facts shared by every caller that decides something from an endpoint."""

from __future__ import annotations

from urllib.parse import urlparse


def host_is(url: str, domain: str) -> bool:
    """True when ``url``'s host is ``domain`` or one of its subdomains.

    Matches the parsed hostname, never a substring of the URL: ``"anthropic.com"
    in url`` also accepts ``https://other.example/anthropic.com`` and
    ``https://notanthropic.com``, and the answer can decide which header a key is
    sent in. A value that does not parse as a URL has no host and is False."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    d = domain.lower().strip(".")
    return bool(host) and (host == d or host.endswith("." + d))
