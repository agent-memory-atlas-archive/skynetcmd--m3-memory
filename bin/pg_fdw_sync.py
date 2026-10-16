#!/usr/bin/env python3
"""pg_fdw_sync.py — PostgreSQL-primary -> PostgreSQL-warehouse fast-path sync.

When BOTH the primary store and the CDW warehouse are PostgreSQL, the row-by-row
SQLite<->PG bridge (pg_sync.py) is the wrong tool: it opens the local side as
sqlite3 and UPSERTs one row at a time in Python. This module uses postgres_fdw to
make the warehouse a set of foreign tables in the primary, so each table's delta
sync is a single server-side ``INSERT INTO <target> SELECT <cols> FROM <source>
WHERE <delta col> > :wm ON CONFLICT (id) DO UPDATE ... <merge guard>`` — bulk,
streaming, no Python row loop. The merge guards are the ones pg_sync uses
(lww_guard / version_guard below).

Directionality: both PUSH (primary -> warehouse) and PULL (warehouse -> primary)
run from the PRIMARY side (which hosts the FDW foreign tables). Push writes the
foreign table; pull reads it. Symmetric SQL, source/target swapped.

Scope: PG->PG only. SQLite source (the default install) and MariaDB stay on
pg_sync.py's generic bridge — sync_all dispatches by backend. This module NEVER
hard-fails the whole sync: if postgres_fdw is absent or the foreign server is
unreachable, it signals the caller to fall back to the generic path.

Requires (one-time, superuser on the PRIMARY pg): CREATE EXTENSION postgres_fdw.
Requires on the WAREHOUSE: the sync role has USAGE + DML on m3_warehouse.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import cast

logger = logging.getLogger("pg_fdw_sync")

# Local schema the warehouse foreign tables are imported into (kept distinct from
# the primary's own public.* so a table name never collides).
FDW_SCHEMA = "cdw_fdw"
FDW_SERVER = "cdw_wh"
WAREHOUSE_SCHEMA = "m3_warehouse"


# ── Merge rules and pull bounds: the ONE definition both sync paths use ──────
# pg_sync (the SQLite<->PG bridge) and this fast path apply the same merges; a
# copy here drifted once (no NULL arm, no curation protection), so a PG primary
# overwrote manual edits the bridge protected. Kept in this module because it
# imports nothing heavy; pg_sync imports these. The text is valid on SQLite and
# PostgreSQL alike (EXCLUDED is case-insensitive on both).


def lww_guard(target: str, ts_col: str = "updated_at", *, protect_curation: bool = False) -> str:
    """ON CONFLICT ... DO UPDATE's WHERE: the incoming row wins when the stored one
    has no timestamp or an older one. With ``protect_curation`` a stored manual or
    system row yields only to an incoming manual one. ``target`` is the (possibly
    schema-qualified) target table, or ``"{target}"`` for a spec template."""
    guard = f"WHERE ({target}.{ts_col} IS NULL OR EXCLUDED.{ts_col} > {target}.{ts_col})"
    if protect_curation:
        guard += f" AND (({target}.change_agent NOT IN ('manual', 'system')) OR (EXCLUDED.change_agent = 'manual'))"
    return guard


def version_guard(target: str) -> str:
    """synchronized_secrets' merge: VERSION precedence, the timestamp breaks ties
    within one version only. Not last-write-wins — reshaping it into one would
    mis-merge the highest-value rows in the store."""
    return (
        f"WHERE EXCLUDED.version > {target}.version "
        f"OR (EXCLUDED.version = {target}.version AND EXCLUDED.updated_at > {target}.updated_at)"
    )


# Where a pull that reads by warehouse ARRIVAL time (wh_arrived_at, pg_002)
# resumes: the warehouse clock, held back to the start of its oldest open
# transaction. A push still in flight stamps its rows after its own transaction
# began, so they land at or after this bound and the next pull reads them. Run on
# a connection to the WAREHOUSE, before the pull's read.
ARRIVAL_BOUND_SQL = (
    "SELECT LEAST(NOW(), COALESCE((SELECT min(xact_start) FROM pg_stat_activity "
    "WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid() AND datname = current_database()), NOW()))"
)


def arrival_bound(cur) -> str | None:
    """The pull bound (ISO) from ``cur``'s server via ARRIVAL_BOUND_SQL; raises on
    a database error — the caller decides how to degrade."""
    cur.execute(ARRIVAL_BOUND_SQL)
    row = cur.fetchone()
    return row[0].isoformat() if row and row[0] else None


# Warehouse tables a pull reads by arrival time. Embeddings and relationships
# carry wh_arrived_at too, but this path reads them in full every run (no delta),
# which is already loss-free.
_ARRIVAL_PULL_TABLES = frozenset({"memory_items"})

# The synced column contract per table — the WAREHOUSE column set (a subset of the
# primary's; belief/KG columns stay local, matching the SQLite bridge). Keeping it
# explicit (not SELECT *) is REQUIRED: primary.memory_items has more columns than
# warehouse.memory_items, so a * would mismatch arity.
_MEMORY_ITEMS_COLS = [
    "id",
    "type",
    "title",
    "content",
    "metadata_json",
    "agent_id",
    "model_id",
    "change_agent",
    "importance",
    "source",
    "origin_device",
    "is_deleted",
    "expires_at",
    "decay_rate",
    "created_at",
    "updated_at",
    "last_accessed_at",
    "access_count",
    "user_id",
    "scope",
    "valid_from",
    "valid_to",
    "content_hash",
    "read_at",
    "conversation_id",
    "refresh_on",
    "refresh_reason",
    "variant",
]

# Per-table sync spec: (table, columns, pk, timestamp_col). Columns are the
# SHARED set (primary ∩ warehouse), schema-verified — NOT guessed. timestamp_col
# drives the delta watermark AND the last-writer-wins guard; None => full-scan
# id-keyed upsert (correct for tables whose shared columns lack updated_at:
# embeddings cascade-delete via memory_items, relationships are immutable).
# Optional 5th element: an explicit conflict guard, for tables whose merge is not
# timestamp last-writer-wins. `{target}` is substituted with the schema-qualified
# target table.
# Heterogeneous by design: (name, columns, pk, [watermark], [conflict guard]).
# Annotated so mypy sees the per-position types instead of collapsing the
# tuple into `str | list[str]`, which made every _upsert() arg look wrong.
_TABLE_SPECS: list[tuple[str, list[str], str, str | None] | tuple[str, list[str], str, str | None, str]] = [
    ("memory_items", _MEMORY_ITEMS_COLS, "id", "updated_at", lww_guard("{target}", protect_curation=True)),
    (
        "memory_embeddings",
        ["id", "memory_id", "embedding", "embed_model", "dim", "created_at", "content_hash", "vector_kind"],
        "id",
        None,
    ),
    ("memory_relationships", ["id", "from_id", "to_id", "relationship_type", "created_at"], "id", None),
    # ── Added to close a SILENT coverage gap ──────────────────────────────────
    # These two were absent while the generic bridge synced them, so a PostgreSQL
    # deployment with the fast path WORKING still lost its tasks and its secrets
    # vault — no error, just tables that never moved. The fast path must never
    # sync less than the slow path; test_fdw_covers_what_the_generic_path_does
    # now fails if the two lists diverge again.
    #
    # Columns are the PRIMARY ∩ WAREHOUSE intersection, verified against both
    # live schemas rather than guessed (they are identical for both tables).
    (
        "tasks",
        [
            "id",
            "title",
            "description",
            "state",
            "owner_agent",
            "created_by",
            "parent_task_id",
            "result_memory_id",
            "metadata_json",
            "created_at",
            "updated_at",
            "completed_at",
            "deleted_at",
        ],
        "id",
        "updated_at",
    ),
    # ⚠ VERSION precedence, not LWW. ts_col stays None so no timestamp delta
    # filter is applied — a secret whose version was bumped without its
    # updated_at moving must still propagate — and the guard is explicit.
    (
        "synchronized_secrets",
        ["service_name", "encrypted_value", "version", "origin_device", "updated_at"],
        "service_name",
        None,
        version_guard("{target}"),
    ),
]


class FdwUnavailable(Exception):
    """Raised when the FDW fast-path can't run (no extension / unreachable server);
    the caller should fall back to the generic pg_sync bridge."""


def _ensure_fdw_wired(pg_cur, warehouse_dsn: str) -> None:
    """Idempotently set up postgres_fdw: extension, foreign server, user mapping,
    and IMPORT FOREIGN SCHEMA. Raises FdwUnavailable if the extension is missing
    (needs superuser to create) or the server can't be reached."""
    from crypto_provider import fips_tier
    from m3_core.dsn import parse_pg_dsn

    # The FDW link is opened by the primary server's libpq, outside this process,
    # so require_pg_transport cannot vet it. FIPS tiers take the bridge, which can.
    if fips_tier() != "off":
        raise FdwUnavailable(
            "FIPS tiers sync through the bridge: postgres_fdw connects from the primary server, "
            "where m3's transport check does not run"
        )

    # libpq's parser (m3_core.dsn): urlparse found no host in a keyword DSN and
    # left a URI's percent-encoded password encoded in the user mapping.
    parts = parse_pg_dsn(warehouse_dsn)
    if parts is None:
        raise FdwUnavailable(
            "observed: the warehouse DSN is not a DSN libpq can parse. inspect: M3_CDW_PG_URL (or the stored PG_URL)"
        )
    host, port = parts.host, str(parts.port)
    dbname, user, password = parts.dbname, parts.user, parts.password

    # Extension — cannot self-create without superuser; probe rather than assume.
    pg_cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgres_fdw'")
    if pg_cur.fetchone() is None:
        raise FdwUnavailable(
            "postgres_fdw extension not installed on the primary (run `CREATE EXTENSION postgres_fdw;` as superuser)"
        )

    # Foreign server, rebuilt each run so a changed DSN (address, sslmode) takes
    # effect: CREATE IF NOT EXISTS kept the first run's options forever. CASCADE
    # drops only the user mapping and foreign tables, both recreated below.
    # hostaddr, sslmode and sslrootcert pass through so the link keeps the DSN's
    # TLS terms; sslrootcert is read on the primary server's host.
    # Omit `host` entirely when the DSN carries none — a unix-socket DSN
    # (`postgresql:///db`, the default for a local cluster with peer auth) has
    # no host component, and passing host=NULL is a SQL syntax error, exactly
    # as it is for password below. Without the option postgres_fdw connects
    # over the local socket, which is what a host-less DSN means.
    server_opts = ["dbname %s", "port %s"]
    server_args: list = [dbname, port]
    if host:
        server_opts.insert(0, "host %s")
        server_args.insert(0, host)
    for name, value in (("hostaddr", parts.hostaddr), ("sslmode", parts.sslmode), ("sslrootcert", parts.sslrootcert)):
        if value:
            server_opts.append(f"{name} %s")
            server_args.append(value)
    pg_cur.execute(f"DROP SERVER IF EXISTS {FDW_SERVER} CASCADE")
    pg_cur.execute(
        f"CREATE SERVER {FDW_SERVER} FOREIGN DATA WRAPPER postgres_fdw OPTIONS ({', '.join(server_opts)})",
        tuple(server_args),
    )
    # User mapping: recreate to pick up a rotated password. current_user maps to
    # the warehouse role. Omit the password option entirely when the DSN carries
    # none (e.g. trust/peer/.pgpass auth) — passing password=NULL is a SQL syntax
    # error, and an empty-string password is not the same as "no password".
    # `user` gets the same treatment: a socket DSN relying on peer auth names no
    # user either, and OPTIONS (user NULL) fails the same way. With neither
    # option the OPTIONS clause is dropped altogether — an empty `OPTIONS ()`
    # is itself a syntax error — and the mapping defaults to the local role.
    pg_cur.execute(f"DROP USER MAPPING IF EXISTS FOR CURRENT_USER SERVER {FDW_SERVER}")
    map_opts = []
    map_args: list = []
    if user:
        map_opts.append("user %s")
        map_args.append(user)
    if password:
        map_opts.append("password %s")
        map_args.append(password)
    if map_opts:
        pg_cur.execute(
            f"CREATE USER MAPPING FOR CURRENT_USER SERVER {FDW_SERVER} OPTIONS ({', '.join(map_opts)})", tuple(map_args)
        )
    else:
        pg_cur.execute(f"CREATE USER MAPPING FOR CURRENT_USER SERVER {FDW_SERVER}")

    pg_cur.execute(f"CREATE SCHEMA IF NOT EXISTS {FDW_SCHEMA}")
    # (Re)import the warehouse tables as foreign tables. IMPORT FOREIGN SCHEMA is
    # not idempotent (errors if a foreign table already exists), so import into a
    # freshly-cleared schema each run — cheap, and picks up warehouse DDL changes.
    tables = ", ".join(t[0] for t in _TABLE_SPECS)
    pg_cur.execute(f"DROP SCHEMA IF EXISTS {FDW_SCHEMA} CASCADE")
    pg_cur.execute(f"CREATE SCHEMA {FDW_SCHEMA}")
    try:
        pg_cur.execute(
            f"IMPORT FOREIGN SCHEMA {WAREHOUSE_SCHEMA} LIMIT TO ({tables}) FROM SERVER {FDW_SERVER} INTO {FDW_SCHEMA}"
        )
    except Exception as e:
        # Connection / auth / permission failures surface here.
        raise FdwUnavailable(f"IMPORT FOREIGN SCHEMA failed: {e}") from e


def _upsert(
    pg_cur,
    target_qual: str,
    source_qual: str,
    cols: list[str],
    pk: str,
    ts_col: str | None,
    watermark: str | None,
    guard: str | None = None,
    delta_col: str | None = None,
) -> int:
    """One set-based delta upsert. target/source are schema-qualified table names.
    Returns rows affected. Last-writer-wins on ts_col (lww_guard) unless `guard`
    overrides it; `delta_col` (default ts_col) is the SOURCE column the watermark
    filters on — wh_arrived_at for a pull that reads by warehouse arrival time.

    `guard` exists because not every table resolves conflicts by timestamp.
    `synchronized_secrets` uses VERSION precedence — the version number is
    authoritative and the timestamp only breaks ties within one version — so
    driving it with the default ts_col guard would silently mis-merge the
    highest-value rows in the store. A caller supplies the real predicate with
    `{target}` as a placeholder for the schema-qualified target name.
    """
    collist = ", ".join(cols)
    non_pk = [c for c in cols if c != pk]
    set_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in non_pk)
    where_delta = ""
    params: list = []
    delta_col = delta_col or ts_col
    if delta_col and watermark:
        where_delta = f" WHERE {delta_col} > %s"
        params.append(watermark)
    if guard:
        conflict_guard = " " + guard.format(target=target_qual)
    else:
        # Last-writer-wins guard only when we have a timestamp to compare.
        conflict_guard = " " + lww_guard(target_qual, ts_col) if ts_col else ""
    sql = (
        f"INSERT INTO {target_qual} ({collist}) "
        f"SELECT {collist} FROM {source_qual}{where_delta} "
        f"ON CONFLICT ({pk}) DO UPDATE SET {set_clause}{conflict_guard}"
    )
    pg_cur.execute(sql, params)
    return cast("int", pg_cur.rowcount)


def sync_pg_to_pg(
    primary_conn,
    warehouse_dsn: str,
    get_wm,
    set_wm,
    dry_run: bool = False,
    warehouse_bound: Callable[[], str | None] | None = None,
) -> dict:
    """Bidirectional PG<->PG warehouse sync via FDW, driven from the primary.

    primary_conn: an open psycopg connection to the PRIMARY store.
    warehouse_dsn: the CDW DSN (M3_CDW_PG_URL) — used for the foreign server.
    get_wm(direction) -> iso str | None ; set_wm(direction, iso) : watermark I/O
      (direction e.g. 'fdw_memory_items_push'). Reuse the caller's sync_watermarks.
    warehouse_bound() -> iso | None: arrival_bound() run on a WAREHOUSE
      connection. Pull watermarks come from it; without one (None, or it fails)
      pull watermarks are not advanced, so the next run re-reads from the last good
      one instead of skipping rows.
    Watermarks are written in primary_conn's transaction, so they commit with the
    rows or not at all. Returns {table: {push: n, pull: n}}. Raises FdwUnavailable
    -> caller falls back.
    """
    results: dict = {}
    with primary_conn.cursor() as cur:
        _ensure_fdw_wired(cur, warehouse_dsn)
        if dry_run:
            primary_conn.rollback()
            return {"dry_run": True, "tables": [t[0] for t in _TABLE_SPECS]}
        # Both bounds are taken BEFORE any read, from the clock of the server
        # whose rows they are compared with — never this host's clock, which is
        # skewed against peers' timestamps and silently skips their rows.
        push_bound = arrival_bound(cur)
        cur.execute("SELECT current_schema()")
        row = cur.fetchone()
        local_schema = row[0] if row and row[0] else None
        if not local_schema:
            raise RuntimeError(
                "observed: current_schema() is NULL on the primary (no schema on its search_path exists). "
                "inspect: the primary role's search_path"
            )
        pull_bound: str | None = None
        if warehouse_bound is not None:
            try:
                pull_bound = warehouse_bound()
            except Exception as e:  # noqa: BLE001 — degrade to "do not advance", reported
                logger.warning(
                    "[fdw] could not read the warehouse clock (%s: %s); pull watermarks are NOT advanced "
                    "this run (the next run re-reads from the last good one). inspect: the warehouse DSN "
                    "(M3_CDW_PG_URL) and that its role can read pg_stat_activity",
                    type(e).__name__,
                    e,
                )
        else:
            logger.warning("[fdw] no warehouse clock supplied; pull watermarks are NOT advanced this run")
        for spec in _TABLE_SPECS:
            # 4- or 5-tuple: the optional 5th element is an explicit conflict
            # guard for tables that do not merge by plain timestamp LWW.
            table, cols, pk, ts_col = spec[:4]
            guard = spec[4] if len(spec) > 4 else None
            local = f'"{local_schema}".{table}'
            foreign = f"{FDW_SCHEMA}.{table}"
            push_key, pull_key = f"fdw_{table}_push", f"fdw_{table}_pull"
            pull_delta = None
            if table in _ARRIVAL_PULL_TABLES:
                # A new key: the old one held this host's clock, compared with
                # writer clocks; the first run on it reads everything once.
                pull_key, pull_delta = f"fdw_{table}_pull_arrival", "wh_arrived_at"
            # PUSH: primary -> warehouse (write the foreign table)
            n_push = _upsert(cur, foreign, local, cols, pk, ts_col, get_wm(push_key), guard)
            # PULL: warehouse -> primary (read the foreign table)
            n_pull = _upsert(cur, local, foreign, cols, pk, ts_col, get_wm(pull_key), guard, pull_delta)
            if push_bound:
                set_wm(push_key, push_bound)
            if pull_bound:
                set_wm(pull_key, pull_bound)
            results[table] = {"push": n_push, "pull": n_pull}
            logger.info("[fdw] %s: pushed=%d pulled=%d", table, n_push, n_pull)
        primary_conn.commit()
    return results
