#!/usr/bin/env python3
"""PostgreSQL PRIMARY-store migration runner — the PG analogue of migrate_memory.py.

The SQLite primary store evolves via numbered ``memory/migrations/NNN_*.up.sql``
files applied by ``migrate_memory.py``. Those files use SQLite-only DDL
(AUTOINCREMENT, FTS5, rowid) and cannot run on PostgreSQL, so PG got a
hand-translated cumulative baseline (``postgres/pg_primary_v1.sql``, stamped
version 39). This runner continues the sequence from 40 with **PG-native**
incremental files:

    memory/migrations/postgres/pg_NNN_<name>.up.sql     (required)
    memory/migrations/postgres/pg_NNN_<name>.down.sql   (optional)

Contract mirrors migrate_memory.py deliberately (discover → order by NNN → track
applied in ``schema_versions`` → apply/stamp; ``down`` reverts + un-stamps), so
the two runners behave the same way. Differences that are intrinsic to the engine:

  * No SAVEPOINT/executescript dance. psycopg2 runs a multi-statement string in one
    real transaction; a file either commits whole or rolls back whole. Migration
    files therefore MUST NOT contain their own COMMIT/ROLLBACK/BEGIN (same rule as
    the SQLite runner; enforced by ``_validate_migration_sql``).
  * DSN resolution goes through the PRIMARY-store resolver + forbidden-host guard
    (``resolve_primary_pg_dsn`` / ``M3_PG_FORBIDDEN_HOSTS``) so the runner can
    NEVER migrate the data-warehouse hub by accident (the PG_URL-split invariant).

Commands: ``up`` (apply pending), ``status``, ``down --to N`` (revert to N),
``plan`` (print pending SQL). ``ensure_schema()`` on the backend still applies the
v39 baseline; this runner takes it from 40 onward. They compose: run the backend's
``ensure_schema`` once to get the baseline, then ``migrate_pg up`` for increments —
or call :func:`run_pending_pg_migrations` programmatically (what the backend does).
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import sys

logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_pg")

# ── Paths ────────────────────────────────────────────────────────────────────
# bin/migrate_pg.py -> repo root is one dir up from bin/.
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PG_MIGRATIONS_DIR = os.path.join(_BASE_DIR, "memory", "migrations", "postgres")
# The warehouse chain (user decision 2026-10-13): applied by pg_sync, in the
# warehouse's own schema, additive only.
_WAREHOUSE_MIGRATIONS_DIR = os.path.join(_BASE_DIR, "memory", "warehouse_migrations")

# The cumulative baseline is version 39 (pg_primary_v1.sql). Incremental files
# continue from 40; anything <= this is owned by the baseline, not this runner.
_BASELINE_VERSION = 39

# PG-native incremental files: pg_NNN_<name>.up.sql / .down.sql (the `pg_` prefix
# distinguishes them from the SQLite NNN_*.sql set living one dir up).
_FNAME_RE = re.compile(r"^pg_(\d+)_(.+?)(?:\.(up|down))?\.sql$")

# Migration files run inside psycopg2's implicit transaction; their own
# transaction-control statements would break atomicity (same rule as the SQLite
# runner). Detected after stripping comments.
_FORBIDDEN_STATEMENTS_RE = re.compile(r"(?im)^\s*(COMMIT|ROLLBACK|BEGIN|START\s+TRANSACTION)\b")


# ── DSN resolution (primary store only, guarded) ─────────────────────────────
def _resolve_dsn(explicit: str | None = None) -> str:
    """Resolve the PRIMARY-store DSN, fail loud if absent.

    ``explicit`` (from --dsn) wins; else the stored primary DSN
    (M3_PRIMARY_PG_URL, the secrets chain; never env). NEVER the warehouse DSN,
    and the result is checked against M3_PG_FORBIDDEN_HOSTS — so this runner cannot touch
    the warehouse hub. Reuses the exact guards from the backend."""
    sys.path.insert(0, os.path.join(_BASE_DIR, "bin"))
    from m3_sdk import PgRole, resolve_pg_dsn
    from memory.backends.postgres_backend import _reject_forbidden_host

    dsn = (explicit or "").strip() or resolve_pg_dsn(PgRole.PRIMARY)
    if not dsn:
        raise SystemExit(
            "No PostgreSQL PRIMARY DSN. Pass --dsn, or store it: `m3 secret set M3_PRIMARY_PG_URL`. "
            "This runner never uses the warehouse DSN."
        )
    _reject_forbidden_host(dsn)  # refuses a warehouse-hub DSN
    return dsn


def _connect(dsn: str):
    try:
        import psycopg2
    except ImportError as e:  # fail loud, actionable
        raise SystemExit(
            "psycopg2 is required for the PG migration runner. Install it: pip install 'psycopg2-binary'."
        ) from e
    return psycopg2.connect(dsn)


# ── Discovery ────────────────────────────────────────────────────────────────
def discover_migrations(migrations_dir: str = _PG_MIGRATIONS_DIR, baseline: int | None = None):
    """Return {version: {'name', 'up': path|None, 'down': path|None}} for every
    pg_NNN_*.sql file. Only versions > the cumulative baseline (39) are runner-
    owned; the baseline file itself (pg_primary_v1.sql) has no NNN prefix and is
    ignored here. Sorted glob so duplicate-prefix resolution is deterministic.

    ``baseline``: the version a chain's baseline file already covers. The default
    applies the main chain's (39) to the main directory only; any other chain (the
    dispatch schema's) has no baseline file, so its pg_001 is runner-owned."""
    if baseline is None:
        baseline = _BASELINE_VERSION if os.path.abspath(migrations_dir) == os.path.abspath(_PG_MIGRATIONS_DIR) else 0
    out: dict[int, dict[str, str | None]] = {}
    seen: dict[tuple[int, str], str] = {}
    for filepath in sorted(glob.glob(os.path.join(migrations_dir, "pg_*.sql"))):
        fname = os.path.basename(filepath)
        m = _FNAME_RE.match(fname)
        if not m:
            # pg_primary_v1.sql and pg_warehouse_*.sql intentionally don't match.
            continue
        version = int(m.group(1))
        if version <= baseline:
            logger.warning(f"Ignoring {fname}: version {version} <= baseline {baseline} (owned by pg_primary_v1.sql).")
            continue
        name = m.group(2)
        direction = m.group(3) or "up"
        key = (version, direction)
        if key in seen:
            logger.warning(
                f"Duplicate pg migration for v{version} {direction}: "
                f"{os.path.basename(seen[key])} vs {fname} — using {fname}."
            )
        seen[key] = filepath
        entry = out.setdefault(version, {"name": name, "up": None, "down": None})
        if direction == "down":
            entry["down"] = filepath
        else:
            entry["up"] = filepath
            entry["name"] = name
    return out


# ── Version tracking ─────────────────────────────────────────────────────────
def _qident(name: str) -> str:
    """A schema name as a quoted SQL identifier (operator-supplied: never raw);
    the dialect seam owns the quoting rule."""
    from memory.backends.dialect import dialect_for

    return dialect_for("postgres").quote_identifier(name)


def get_applied_versions(conn, schema: str = "public") -> list[int]:
    """Integer version markers from ``<schema>.schema_versions`` (skips non-numeric
    bench labels), or [] if the table is absent (baseline not yet applied). Each
    chain keeps its own table: the dispatch schema's versions are not the main
    store's."""
    table = f"{_qident(schema)}.schema_versions"
    cur = conn.cursor()
    cur.execute("SELECT to_regclass(%s)", (table,))
    if cur.fetchone()[0] is None:
        return []
    cur.execute(f"SELECT version FROM {table} ORDER BY version")
    out: list[int] = []
    for (v,) in cur.fetchall():
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def current_version(conn) -> int:
    applied = get_applied_versions(conn)
    return max(applied) if applied else 0


# ── Validation / apply / revert ──────────────────────────────────────────────
def _validate_migration_sql(filepath: str, sql: str) -> None:
    """Reject files that issue their own COMMIT/ROLLBACK/BEGIN — those break the
    single-transaction-per-file atomicity. Comments stripped first so prose using
    those words doesn't trip it."""
    stripped = re.sub(r"--[^\n]*", "", sql)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    m = _FORBIDDEN_STATEMENTS_RE.search(stripped)
    if m:
        raise ValueError(
            f"Migration {os.path.basename(filepath)} contains a top-level "
            f"{m.group(1).upper()} statement, which conflicts with the "
            f"one-transaction-per-file model of migrate_pg.py. Remove it — each "
            f"file runs inside an implicit transaction."
        )


def apply_migration(conn, version: int, up_path: str) -> None:
    """Apply one up file atomically and stamp schema_versions. All-or-nothing:
    psycopg2 runs the whole script in one transaction; on error we roll back and
    the version is NOT stamped."""
    logger.info(f"Applying pg migration {version}: {os.path.basename(up_path)}")
    with open(up_path, encoding="utf-8") as f:
        sql = f.read()
    _validate_migration_sql(up_path, sql)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_versions (version, filename) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
            (version, os.path.basename(up_path)),
        )
        conn.commit()
        logger.info(f"  -> applied v{version}")
    except Exception:
        conn.rollback()
        logger.error(f"Failed to apply v{version}; rolled back.")
        raise


def revert_migration(conn, version: int, down_path: str) -> None:
    """Run a down file and un-stamp the version, atomically."""
    logger.info(f"Reverting pg migration {version}: {os.path.basename(down_path)}")
    with open(down_path, encoding="utf-8") as f:
        sql = f.read()
    _validate_migration_sql(down_path, sql)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cur.execute("DELETE FROM schema_versions WHERE version = %s", (version,))
        conn.commit()
        logger.info(f"  -> reverted v{version}")
    except Exception:
        conn.rollback()
        logger.error(f"Failed to revert v{version}; rolled back.")
        raise


# ── Programmatic entry (used by the backend's ensure_schema) ─────────────────
def run_pending_pg_migrations(conn, migrations_dir: str = _PG_MIGRATIONS_DIR, schema: str | None = None) -> list[int]:
    """Apply every pending pg_NNN migration (> current applied version) in order,
    on an already-open connection. Returns the list of versions applied (possibly
    empty). Raises on the first failure (that file rolled back; earlier ones stay
    committed). Idempotent — a second call with nothing pending returns [].

    ``schema``: a chain that lives in its own schema (the dispatch store). The
    schema is created if absent and the files run with it as the SESSION
    search_path (a LOCAL one would end at each file's commit), so their unqualified
    DDL and the version stamps land there; the previous search_path is restored."""
    migs = discover_migrations(migrations_dir)
    if not migs:
        return []
    prev_path = None
    if schema is not None:
        cur = conn.cursor()
        cur.execute("SHOW search_path")
        prev_path = cur.fetchone()[0]
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_qident(schema)}")
        cur.execute(f"SET search_path TO {_qident(schema)}")
        conn.commit()
    try:
        applied = set(get_applied_versions(conn, schema or "public"))
        pending = [v for v in sorted(migs) if v not in applied]
        done: list[int] = []
        for v in pending:
            up = migs[v]["up"]
            if not up:
                logger.error(f"pg migration v{v} has no up file — stopping.")
                break
            apply_migration(conn, v, up)
            done.append(v)
        return done
    finally:
        if prev_path is not None:
            try:
                conn.rollback()  # leave no aborted transaction behind a failure
                cur = conn.cursor()
                cur.execute(f"SET search_path TO {prev_path}")
                conn.commit()
            except Exception:  # noqa: BLE001 -- restoring must not mask the real error
                logger.warning("could not restore search_path to %r after the %s migrations", prev_path, schema)


_WAREHOUSE_LOCK_KEY = "m3_warehouse_migrations"  # pg_advisory_lock(hashtext(...))
_warned_behind: set[str] = set()


def run_warehouse_migrations(conn, schema: str) -> list[int]:
    """Bring the warehouse (its tables in ``schema``: m3_warehouse, or public on an
    older layout) to the current schema. Called by pg_sync before every run.

    Under a session advisory lock, so two machines starting a sync at once cannot
    both apply a migration. Additive only, so a machine whose code knows FEWER
    versions than the warehouse has keeps syncing: it logs that once and goes on.
    Returns the versions applied."""
    known = max(discover_migrations(_WAREHOUSE_MIGRATIONS_DIR) or {0: None})
    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (_WAREHOUSE_LOCK_KEY,))
    try:
        applied = get_applied_versions(conn, schema)
        if applied and max(applied) > known and schema not in _warned_behind:
            _warned_behind.add(schema)
            logger.info(
                f"warehouse schema {schema!r} is at v{max(applied)}; this m3 knows up to v{known}. "
                "Warehouse changes are additive, so syncing continues; upgrade m3 to use the newer columns."
            )
        return run_pending_pg_migrations(conn, _WAREHOUSE_MIGRATIONS_DIR, schema=schema)
    finally:
        try:
            conn.rollback()
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_WAREHOUSE_LOCK_KEY,))
            conn.commit()
        except Exception:  # noqa: BLE001 -- the lock also ends with the session
            logger.warning("could not release the warehouse migration lock; it ends with this connection")


# ── CLI subcommands ──────────────────────────────────────────────────────────
def cmd_status(args) -> None:
    migs = discover_migrations()
    conn = _connect(_resolve_dsn(args.dsn))
    try:
        applied = set(get_applied_versions(conn))
        cur = current_version(conn)
    finally:
        conn.close()
    print(f"Baseline (pg_primary_v1.sql): v{_BASELINE_VERSION}")
    print(f"Current applied version:      v{cur}")
    if not migs:
        print("Incremental pg migrations:    (none on disk)")
        return
    print("Incremental pg migrations:")
    for v in sorted(migs):
        mark = "[x]" if v in applied else "[ ]"
        down = "down" if migs[v]["down"] else "no-down"
        print(f"  {mark} v{v}  {migs[v]['name']}  ({down})")


def cmd_plan(args) -> None:
    migs = discover_migrations()
    conn = _connect(_resolve_dsn(args.dsn))
    try:
        applied = set(get_applied_versions(conn))
    finally:
        conn.close()
    pending = [v for v in sorted(migs) if v not in applied]
    if not pending:
        print("No pending pg migrations.")
        return
    print(f"--- pending pg migrations ({len(pending)}) ---")
    for v in pending:
        up = migs[v]["up"]
        print(f"\n# v{v} ({migs[v]['name']}): {os.path.basename(up) if up else '<no up>'}")
        if up:
            with open(up, encoding="utf-8") as f:
                print(f.read().rstrip())
    print("\n--- end of plan ---")


def cmd_up(args) -> None:
    conn = _connect(_resolve_dsn(args.dsn))
    try:
        migs = discover_migrations()
        applied = set(get_applied_versions(conn))
        cur = current_version(conn)
        all_versions = sorted(migs)
        target_ver = args.to if args.to is not None else (max(all_versions) if all_versions else cur)
        pending = [v for v in all_versions if v not in applied and v <= target_ver]
        if not pending:
            logger.info("Database is up to date. No pending pg migrations.")
            return
        print(f"\nCurrent version: v{cur}")
        print(f"Target version:  v{target_ver}")
        print("Will apply:")
        for v in pending:
            down = "has down" if migs[v]["down"] else "NO down (irreversible)"
            print(f"  + v{v}  {migs[v]['name']}  [{down}]")
        if args.dry_run:
            logger.info("Dry run — no changes made.")
            return
        if not args.yes:
            resp = input(f"\nApply {len(pending)} pg migration(s) to the PRIMARY store? [y/N] ")
            if resp.strip().lower() not in ("y", "yes"):
                logger.info("Aborted by user.")
                return
        for v in pending:
            up = migs[v]["up"]
            if not up:
                logger.error(f"v{v} has no up file — skipping")
                continue
            apply_migration(conn, v, up)
        logger.info(f"Done. Now at v{current_version(conn)}.")
    finally:
        conn.close()


def cmd_down(args) -> None:
    if args.to is None:
        raise SystemExit("down requires --to N (the version to roll back TO)")
    conn = _connect(_resolve_dsn(args.dsn))
    try:
        migs = discover_migrations()
        applied = sorted(get_applied_versions(conn))
        # Revert versions strictly greater than the target, newest first.
        to_revert = [v for v in reversed(applied) if v > args.to and v > _BASELINE_VERSION]
        if not to_revert:
            logger.info(f"Nothing to revert above v{args.to}.")
            return
        missing = [v for v in to_revert if not (migs.get(v) or {}).get("down")]
        if missing:
            raise SystemExit(
                f"Cannot roll back: no down file for version(s) {missing}. "
                f"Add pg_NNN_*.down.sql or choose a higher --to."
            )
        print("Will revert (newest first):")
        for v in to_revert:
            print(f"  - v{v}  {migs[v]['name']}")
        if not args.yes:
            resp = input(f"\nRevert {len(to_revert)} pg migration(s)? [y/N] ")
            if resp.strip().lower() not in ("y", "yes"):
                logger.info("Aborted by user.")
                return
        for v in to_revert:
            revert_migration(conn, v, migs[v]["down"])
        logger.info(f"Done. Now at v{current_version(conn)}.")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PostgreSQL PRIMARY-store migration runner.")
    p.add_argument("--dsn", default=None, help="Explicit DSN (else M3_PRIMARY_PG_URL/M3_PG_URL).")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_up = sub.add_parser("up", help="Apply pending pg migrations.")
    p_up.add_argument("--to", type=int, default=None, help="Stop at this version.")
    p_up.add_argument("--yes", action="store_true", help="Skip confirmation.")
    p_up.add_argument("--dry-run", action="store_true", help="Show plan, apply nothing.")
    p_up.set_defaults(func=cmd_up)

    p_st = sub.add_parser("status", help="Show applied vs pending pg migrations.")
    p_st.set_defaults(func=cmd_status)

    p_pl = sub.add_parser("plan", help="Print the SQL of pending pg migrations.")
    p_pl.set_defaults(func=cmd_plan)

    p_dn = sub.add_parser("down", help="Revert pg migrations down to --to N.")
    p_dn.add_argument("--to", type=int, default=None, help="Version to roll back TO (required).")
    p_dn.add_argument("--yes", action="store_true", help="Skip confirmation.")
    p_dn.set_defaults(func=cmd_down)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
