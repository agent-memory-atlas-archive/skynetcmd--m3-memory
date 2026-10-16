"""Open PostgreSQL connections: the one place m3 does (DESIGN_PHILOSOPHIES §10a).

Every connection is checked against the active FIPS tier first
(``crypto_provider.require_pg_transport``): under ``M3_FIPS_MODE`` a connection
that crosses a network must use ``sslmode=verify-full``; under ``M3_FIPS_STRICT``
it must also run on an attested validated TLS library, not psycopg2-binary's
bundled OpenSSL. Outside FIPS tiers this is a plain ``psycopg2.connect``.

``tests/test_pg_connect_one_owner.py`` keeps other modules from calling
``psycopg2.connect`` directly, so the check cannot be bypassed by a new site.
"""

from __future__ import annotations

from typing import Any


def check_dsn(dsn: str) -> None:
    """Raise ``crypto_provider.FipsTransportError`` if the FIPS tier forbids ``dsn``."""
    from crypto_provider import require_pg_transport

    require_pg_transport(dsn)


def pg_connect(dsn: str, **kwargs: Any):
    """``psycopg2.connect(dsn, **kwargs)`` after the FIPS transport check."""
    check_dsn(dsn)
    import psycopg2

    return psycopg2.connect(dsn, **kwargs)


def pg_pool(minconn: int, maxconn: int, dsn: str, **kwargs: Any):
    """A ``ThreadedConnectionPool`` after the FIPS transport check."""
    check_dsn(dsn)
    from psycopg2.pool import ThreadedConnectionPool

    return ThreadedConnectionPool(minconn, maxconn, dsn=dsn, **kwargs)
