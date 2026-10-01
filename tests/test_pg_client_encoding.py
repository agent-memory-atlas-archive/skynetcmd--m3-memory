"""The PG pool must pin `client_encoding=UTF8` rather than inherit the server's.

psycopg2 adopts the SERVER encoding for the client. Against a SQL_ASCII cluster
that makes every non-ASCII character m3 writes raise `UnicodeEncodeError` inside
the driver — and m3's own payload is full of them, so the failure arrives during
the very first schema load, pointing at the driver rather than at the real cause.

A cluster becomes SQL_ASCII without anyone choosing it. Measured 2026-09-30 on a
minimal Debian 13 LXC: the image carries no UTF-8 locale, so `apt install
postgresql` runs initdb under LC_CTYPE=C and the cluster comes up SQL_ASCII.
The only hint is a perl locale warning buried in apt's output. The resulting
suite run showed 17 failures and 65 errors, all `UnicodeEncodeError`, on a
codebase that was perfectly healthy — the symptom pointed nowhere near encoding.

These are unit tests on purpose: no live PostgreSQL, so the contract is pinned on
every OS and every Python in the matrix, including the runners that have no PG.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bin"))

# `memory.backends` imports the whole memory package, which pulls in yaml and
# the rest of the core deps — absent on a bare checkout. The capability marker
# auto-skips in that case (conftest's `requires_memory_core`); psycopg2 gets its
# own guard because the pool class under test is psycopg2's.
pytestmark = pytest.mark.requires_memory_core

psycopg2 = pytest.importorskip("psycopg2", reason="the pool constructor is psycopg2's")
pytest.importorskip("psycopg2.pool")


class _FakePool:
    """Captures what the backend asked psycopg2 for."""

    last: dict | None = None

    def __init__(self, minconn, maxconn, **kwargs):
        type(self).last = {"minconn": minconn, "maxconn": maxconn, **kwargs}


@pytest.fixture()
def captured(monkeypatch):
    """Intercept pool construction; no server is contacted."""
    import psycopg2.pool

    _FakePool.last = None
    monkeypatch.setattr(psycopg2.pool, "ThreadedConnectionPool", _FakePool)
    return _FakePool


def _build(dsn: str, captured) -> dict:
    from memory.backends.postgres_backend import PostgresBackend

    backend = PostgresBackend(dsn=dsn)
    backend._ensure_pool()
    assert captured.last is not None, "the pool was never constructed"
    return captured.last


def test_client_encoding_is_pinned_to_utf8(captured):
    """THE regression. Without this the driver inherits SQL_ASCII and dies on
    the first em dash in m3's own schema."""
    got = _build("postgresql://u:p@127.0.0.1:5432/db", captured)
    assert got.get("client_encoding") == "UTF8", (
        "the pool must pin client_encoding; inheriting the server's makes a "
        f"SQL_ASCII cluster fail on any non-ASCII text. got: {got!r}"
    )


def test_the_dsn_is_still_passed_through(captured):
    """Pinning the encoding must not displace the DSN."""
    dsn = "postgresql://u:p@127.0.0.1:5432/db"
    assert _build(dsn, captured)["dsn"] == dsn


@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@h:5432/db?client_encoding=LATIN1",
    "postgresql://u:p@h:5432/db?client_encoding=UTF8",
    "host=h dbname=db client_encoding=LATIN1",
    # NOT tested: an uppercase CLIENT_ENCODING keyword. libpq conninfo keywords
    # are lowercase-only, so it is an invalid DSN that connect() rejects however
    # we behave — asserting anything there would pin undefined behaviour.
])
def test_an_explicit_choice_in_the_dsn_wins(dsn, captured):
    """Never override the operator. Passing it twice would also hand libpq a
    duplicate keyword, where last-one-wins silently decides which applies."""
    assert "client_encoding" not in _build(dsn, captured), (
        f"the DSN already sets client_encoding ({dsn!r}); the pool must not "
        "also pass one"
    )


def test_a_dsn_mentioning_encoding_elsewhere_is_not_mistaken_for_a_choice(captured):
    """The guard is a substring test, so pin what it must NOT match: a database
    or password that merely contains the words."""
    got = _build("postgresql://u:p@h:5432/client_encoding_notes", captured)
    assert got.get("client_encoding") == "UTF8", (
        "a database NAMED like the option is not the option being set"
    )
