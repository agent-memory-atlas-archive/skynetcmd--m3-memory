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


def utc_session(conn):
    """Set ``conn``'s session time zone to UTC and return it.

    PostgreSQL renders a TIMESTAMPTZ in the SESSION time zone, which defaults to
    the server's: on a server set to America/New_York every timestamp m3 read
    came back as -04:00. m3 stores and reports UTC. A SET after connecting, not
    a startup ``options`` parameter, which PgBouncer refuses."""
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
    if not conn.autocommit:
        conn.commit()  # a fresh connection: nothing but the SET to commit
    return conn


def pg_connect(dsn: str, **kwargs: Any):
    """``psycopg2.connect(dsn, **kwargs)`` after the FIPS transport check, in UTC."""
    check_dsn(dsn)
    import psycopg2

    return utc_session(psycopg2.connect(dsn, **kwargs))


def pg_pool(minconn: int, maxconn: int, dsn: str, **kwargs: Any):
    """A ``ThreadedConnectionPool`` after the FIPS transport check, whose
    connections are in UTC."""
    check_dsn(dsn)
    from psycopg2.pool import ThreadedConnectionPool

    class _UtcPool(ThreadedConnectionPool):
        def _connect(self, key=None):
            return utc_session(super()._connect(key))

    return _UtcPool(minconn, maxconn, dsn=dsn, **kwargs)
