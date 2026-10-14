"""Bring the PostgreSQL data warehouse to its current schema, or report where it is.

The warehouse schema comes from its migration chain (memory/warehouse_migrations/),
which pg_sync applies automatically before every run.
This is the manual entry point for the same thing:

    python bin/pg_setup.py           # apply pending warehouse migrations
    python bin/pg_setup.py --check   # report the layout and versions; change nothing

The warehouse DSN is the M3_CDW_PG_URL secret (m3 secret set M3_CDW_PG_URL).
"""

import argparse
import logging
import sys

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
logger = logging.getLogger("pg_setup")


def _get_pg_url() -> str:
    """The data-warehouse DSN (m3_core.secret_store.resolve_pg_dsn, warehouse role):
    the secrets file, then the keyring, then the vault. Never the environment."""
    from m3_core.secret_store import PgRole, resolve_pg_dsn

    url = resolve_pg_dsn(PgRole.WAREHOUSE)
    if url:
        return url
    logger.error(
        "Data-warehouse DSN not found. observed: no M3_CDW_PG_URL secret (or legacy PG_URL vault entry). "
        "fix: m3 secret set M3_CDW_PG_URL   (m3 config import-env moves one set in the environment)"
    )
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="report the layout and versions; change nothing")
    args = parser.parse_args()

    import migrate_pg
    import pg_sync

    try:
        conn = psycopg2.connect(_get_pg_url())
    except Exception as e:
        logger.error(f"could not connect to the warehouse: {type(e).__name__}: {e}")
        return 1
    try:
        layout = pg_sync._warehouse_layout(conn)
        available = sorted(migrate_pg.discover_migrations(migrate_pg._WAREHOUSE_MIGRATIONS_DIR))
        applied = migrate_pg.get_applied_versions(conn, layout)
        pending = [v for v in available if v not in applied]
        if args.check:
            print(f"warehouse schema: {layout}")
            print(f"applied: {applied or 'none'}")
            print(f"pending: {pending or 'none'}")
            return 0
        done = migrate_pg.run_warehouse_migrations(conn, layout)
        logger.info(f"warehouse ({layout}): applied {done or 'nothing, already current'}")
        return 0
    except Exception as e:
        logger.error(f"warehouse setup failed: {type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
