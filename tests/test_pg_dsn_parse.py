"""m3_core.dsn: one DSN parser for both libpq forms (URI and keyword/value).

urlparse reads only the URI form. Given `host=h dbname=d password=p` it returns
the whole text as the path, so the hand-rolled parsers this replaced saw no host,
took the password for part of the database name, and judged two spellings of one
database different. Each case below failed before (found while testing a named
store on a second PostgreSQL database).
"""

from __future__ import annotations

import pytest
from m3_core.dsn import DsnParts, parse_pg_dsn
from m3_core.secret_store import set_secret  # noqa: E402

_SECRET = "s3cr3t-pw"


@pytest.mark.parametrize(
    "dsn,host,port,db",
    [
        (f"postgresql://u:{_SECRET}@H:5433/db/", "H", 5433, "db"),
        ("postgresql:///db", "", 5432, "db"),  # socket, no host or user
        (f"user=u password={_SECRET} dbname=db host=h port=5432", "h", 5432, "db"),
        ("dbname=db", "", 5432, "db"),
        ("postgresql://u@%2Fvar%2Frun%2Fpostgresql/db", "/var/run/postgresql", 5432, "db"),
        ("host=a,b dbname=x", "a,b", 5432, "x"),  # libpq multi-host
    ],
)
def test_every_libpq_shape_parses(dsn, host, port, db):
    parts = parse_pg_dsn(dsn)
    assert parts is not None and (parts.host, parts.port, parts.dbname) == (host, port, db)


@pytest.mark.parametrize("dsn", ["", "   ", "not a dsn", "host=h port=notaport dbname=d"])
def test_what_libpq_rejects_is_none(dsn):
    assert parse_pg_dsn(dsn) is None


def test_a_uri_password_is_decoded_like_libpq_does():
    assert parse_pg_dsn("postgresql://u:p%40ss@h/db").password == "p@ss"


def test_both_forms_of_one_database_have_one_identity():
    uri = parse_pg_dsn(f"postgresql://alice:{_SECRET}@DB.example:5432/m3")
    kw = parse_pg_dsn("host=db.example dbname=m3 user=bob")
    assert isinstance(uri, DsnParts) and isinstance(kw, DsnParts)
    assert uri.identity() == kw.identity() == ("db.example", 5432, "m3")


def test_store_identity_never_carries_the_password():
    from memory.backends.postgres_backend import PostgresBackend

    ident = PostgresBackend(dsn=f"user=u password={_SECRET} dbname=m3 host=h").store_identity()
    assert ident == "postgres:h:5432/m3"
    assert _SECRET not in ident


def test_the_warehouse_guard_sees_through_the_keyword_form(monkeypatch):
    from memory.backends.postgres_backend import _reject_same_as_warehouse

    set_secret("M3_CDW_PG_URL", f"postgresql://wh:{_SECRET}@wh.example:5432/cdw")
    with pytest.raises(RuntimeError, match="SAME database as the warehouse") as e:
        _reject_same_as_warehouse(f"host=wh.example dbname=cdw user=me password={_SECRET}")
    assert _SECRET not in str(e.value)
    _reject_same_as_warehouse("host=wh.example dbname=primary user=me")  # another database: fine


@pytest.mark.parametrize(
    "dsn",
    [
        "host=wh.example dbname=m3 user=u",
        "host=ok.example,wh.example dbname=m3",  # libpq would try the forbidden host too
        "postgresql://u@WH.example/m3",
    ],
)
def test_a_forbidden_host_is_refused_in_any_form(m3_settings, dsn):
    from memory.backends.postgres_backend import _reject_forbidden_host

    m3_settings({"db.postgres.forbidden_hosts": ["wh.example"]})
    with pytest.raises(RuntimeError, match="forbidden"):
        _reject_forbidden_host(dsn)


def test_a_forbidden_host_inside_a_password_is_not_a_hit(m3_settings):
    from memory.backends.postgres_backend import _reject_forbidden_host

    m3_settings({"db.postgres.forbidden_hosts": ["wh.example"]})
    _reject_forbidden_host("host=ok.example dbname=m3 password=wh.example")


def test_sync_finds_the_host_of_a_keyword_warehouse_dsn(monkeypatch):
    import sync_all

    monkeypatch.setattr(sync_all, "_warehouse_host_from_env", lambda: None)
    import m3_sdk

    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn", lambda: "host=wh.example port=6432 dbname=cdw")
    assert sync_all.warehouse_target() == ("wh.example", 6432, "warehouse DSN")


def test_hostaddr_is_what_libpq_dials_and_host_names_the_certificate():
    parts = parse_pg_dsn("postgresql://u@db.example:5432/m3?hostaddr=192.0.2.7&sslmode=verify-full&sslrootcert=/ca.pem")
    assert parts is not None
    assert (parts.host, parts.hostaddr, parts.sslrootcert) == ("db.example", "192.0.2.7", "/ca.pem")
    assert parts.dial_hosts() == ["192.0.2.7"]
    assert parse_pg_dsn("host=a,b dbname=x").dial_hosts() == ["a", "b"]


def test_a_local_name_with_a_remote_hostaddr_is_not_local():
    """libpq dials the hostaddr, so calling this local skipped the FIPS TLS rule."""
    assert not parse_pg_dsn("host=localhost hostaddr=192.0.2.7 dbname=m3").is_local()
    assert parse_pg_dsn("host=db.example hostaddr=127.0.0.1 dbname=m3").is_local()


def test_a_forbidden_hostaddr_is_refused(m3_settings):
    from memory.backends.postgres_backend import _reject_forbidden_host

    m3_settings({"db.postgres.forbidden_hosts": ["192.0.2.7"]})
    with pytest.raises(RuntimeError, match="forbidden"):
        _reject_forbidden_host("host=ok.example hostaddr=192.0.2.7 dbname=m3")
