"""PostgreSQL DSN parsing: the one owner (DESIGN_PHILOSOPHIES §10a).

A DSN arrives in either libpq form, a URI (``postgresql://u:p@h:5432/db``) or
keyword/value (``host=h port=5432 dbname=db user=u password=p``). ``urlparse``
reads only the first: given the second it finds no scheme or host and returns the
WHOLE string as the path, so a hand-rolled parser saw no host, called the password
part of the database name, and judged two spellings of one database different.
That hid the warehouse from the same-database guard, put the password into
``store_identity()``, and skipped warehouse sync with a "no host" warning.

``parse_dsn`` from psycopg2 (a core dependency) is libpq's own parser, the one
``connect()`` uses, so this module agrees with the driver on every shape.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PORT = 5432


@dataclass(frozen=True, slots=True, kw_only=True)
class DsnParts:
    host: str  # "" for the libpq default (a local socket); may be "/run/postgresql" or "a,b"
    port: int
    dbname: str
    user: str | None = None
    password: str | None = None

    def identity(self) -> tuple[str, int, str]:
        """``(host, port, dbname)``: which database, whatever the credentials."""
        return (self.host.lower(), self.port, self.dbname)


def parse_pg_dsn(dsn: str) -> DsnParts | None:
    """The parts of ``dsn`` in either libpq form; None when libpq rejects it or it
    names neither a host nor a database."""
    text = (dsn or "").strip()
    if not text:
        return None
    try:
        from psycopg2.extensions import parse_dsn

        raw = parse_dsn(text)
        port = int(raw["port"]) if raw.get("port") else DEFAULT_PORT
    except Exception:  # noqa: BLE001 — libpq rejected it (ProgrammingError) or a bad port
        return None
    host = raw.get("host") or raw.get("hostaddr") or ""
    dbname = (raw.get("dbname") or "").strip("/")
    if not host and not dbname:
        return None
    return DsnParts(host=host, port=port, dbname=dbname,
                    user=raw.get("user"), password=raw.get("password"))
