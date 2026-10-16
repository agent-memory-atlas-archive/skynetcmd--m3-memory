#!/usr/bin/env python3
"""
sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL).
Runs pg_sync.py once per configured DB. Offline-tolerant.
Safe to call on any platform; skips gracefully if target unreachable or DB absent.

Usage:
    python bin/sync_all.py
    python bin/sync_all.py --dry-run   (connectivity check only)

DB list:
    The resolved main store (m3_sdk.resolve_db_path). The agent_memory manifest
    sweeps both `main` and `chatlog` targets internally, so chatlog data gets
    synced in the same pass without listing it separately. Bench DBs and other
    custom databases are NOT auto-detected — set M3_SYNC_DBS to include them.

    Example self-host override:
        M3_SYNC_DBS=memory/agent_memory.db:../m3-memory-bench/data/agent_bench.db
"""

from m3_core.paths import apply_early_argv as _apply_early_argv
from m3_core.paths import pin_database  # noqa: E402

# --config FILE (the file this job was registered with) is pinned, and every
# --set KEY=VALUE applied, before any import below binds a root or reads a
# setting at import time (chatlog_config, log dirs).
_apply_early_argv()

import argparse
import logging
import os
import pathlib
import socket
import subprocess
import sys

from m3_core.config import settings as _m3cfg
from m3_core.seams import is_windows, os_name

IS_WIN = is_windows()

# Subprocess timeout for pg_sync.py. First-run full syncs can take several
# minutes; delta syncs are much faster. Override with M3_PG_SYNC_TIMEOUT (seconds).
PG_SYNC_TIMEOUT = _m3cfg.setting("sync.timeout_s")

BASE = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(BASE / "bin"))
from m3_core.paths import get_m3_logs_root  # noqa: E402

LOG_DIR = pathlib.Path(get_m3_logs_root())
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "sync_all.log"

# "ran cleanly, replicated nothing" — EX_TEMPFAIL. Imported from pg_sync rather
# than restated: the two processes agree on this number across a subprocess
# boundary, and a second literal would drift the day one of them changes (§10a).
try:
    sys.path.insert(0, str(BASE / "bin"))
    from pg_sync import _EXIT_SKIPPED
except Exception:  # noqa: BLE001 — pg_sync pulls heavy deps; never block startup
    _EXIT_SKIPPED = 75


def _resolve_python() -> str:
    """The interpreter to run the pg_sync.py subprocess with. Use the CURRENT
    interpreter (sys.executable) — it is correct for every install layout: a
    pipx venv (no in-tree .venv), an editable/dev tree, or a system install.
    The old hardcoded BASE/.venv guess only existed in a dev checkout and 404'd
    for pipx users (FileNotFoundError [WinError 2] -> the scheduled sync task
    failed silently). Fall back to the .venv guess only if sys.executable is
    somehow unavailable."""
    exe = sys.executable
    if exe and os.path.exists(pathlib.Path(exe)):
        return exe
    guess = BASE / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
    return str(guess)


PY = _resolve_python()


def _warehouse_host_from_env() -> str:
    """The host env vars, read when asked: a value captured at import is the
    importing process's environment, not the one sync runs in."""
    return (_m3cfg.setting("sync.warehouse_host") or _m3cfg.setting("sync.target_ip") or "").strip()


def warehouse_target() -> "tuple[str, int, str] | None":
    """(host, port, source) of the warehouse to sync with, or None if none is
    configured. The host comes from the warehouse DSN, the address sync connects
    to; the host settings answer only without one. A host setting naming another
    address made the reachability probe test a path sync does not use."""
    host = _warehouse_host_from_env()
    try:
        from m3_sdk import resolve_warehouse_dsn

        dsn = resolve_warehouse_dsn()
    except Exception as e:  # noqa: BLE001 — reported, then treated as unconfigured
        log.warning(f"could not read the warehouse DSN ({type(e).__name__}: {e}); sync is skipped. inspect: m3 doctor")
        return None
    if not dsn:
        return (host, 5432, "M3_POSTGRES_SERVER") if host else None
    from m3_core.dsn import parse_pg_dsn

    # libpq's parser (m3_core.dsn): urlsplit found no host in a keyword DSN
    # (`host=h dbname=d`), so this skipped sync with a false "no host" warning.
    parts = parse_pg_dsn(dsn)
    if parts is None or not parts.host:
        why = "is not a DSN libpq can parse" if parts is None else "has no host"
        log.warning(f"the warehouse DSN {why}; sync is skipped. inspect: M3_CDW_PG_URL or the stored PG_URL")
        return None
    # The address libpq dials: hostaddr when set (host then only names the
    # certificate, and may not resolve here), else the host list.
    return ",".join(parts.dial_hosts()), parts.port, "warehouse DSN"


# Logging is configured in main() via setup_task_runtime so scheduled-task
# runs self-log without a shell `>>` redirect. This minimal fallback keeps
# `log` usable if a caller imports this module without running main().
log = logging.getLogger("sync_all")

# ── DB list resolution ────────────────────────────────────────────────────────
# The resolved main store. The agent_memory.yaml manifest already sweeps BOTH
# `main` (agent_memory.db) and `chatlog` (agent_chatlog.db) targets in a single
# pg_sync invocation, so we don't list chatlog separately — doing so would either
# re-sync the same data or fail on a missing manifest. Anything beyond this
# (bench DBs, custom layouts) is self-host territory: M3_SYNC_DBS.

# Why this run replicated nothing, one entry per skipped part. A skip is neither
# success nor failure (EX_TEMPFAIL); main() names every reason instead of one
# canned cause.
_SKIP_REASONS: list[str] = []


def _resolve_dbs() -> list[pathlib.Path]:
    """Return list of DB paths to sync.

    Priority:
      1. M3_SYNC_DBS env var (explicit override; paths separated by commas or the
         OS path separator -- ':' on POSIX, ';' on Windows, where ':' appears in
         every drive-letter path).
      2. m3_sdk.resolve_db_path() — the Homecoming-aware resolver (honours
         M3_ENGINE_ROOT / M3_MEMORY_ROOT / M3_DATABASE). Its failure propagates:
         a repo-relative guess synced a store nobody uses.

    Bench DBs and any other databases are NOT auto-detected — set M3_SYNC_DBS
    if you self-host a custom layout.
    """
    raw = os.environ.get("M3_SYNC_DBS", "")
    if raw:
        parts = [p.strip() for p in raw.replace(",", os.pathsep).split(os.pathsep) if p.strip()]
        resolved = []
        for p in parts:
            path = pathlib.Path(p)
            if not path.is_absolute():
                path = BASE / path
            resolved.append(path.resolve())
        return resolved

    # No explicit override — use the Homecoming-aware DB resolver so we always
    # sync the live database.
    from m3_sdk import resolve_db_path

    return [pathlib.Path(resolve_db_path()).resolve()]


# ── Network check ─────────────────────────────────────────────────────────────


def is_reachable(host: str, port: int = 5432, timeout: float = 3.0) -> bool:
    """TCP probe — faster and more reliable than ping across platforms. A
    comma-separated list (libpq's multi-host form) is reachable when any is."""
    for h in (part.strip() for part in host.split(",")):
        try:
            with socket.create_connection((h, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


# ── pg_sync runner ────────────────────────────────────────────────────────────


def run_pg_sync_for_db(db_path: pathlib.Path, dry_run: bool) -> bool | None:
    """Run pg_sync.py --db <path> for one database. True on success, False on
    failure, None when skipped (nothing replicated; the reason is recorded)."""
    if not os.path.exists(db_path):
        # A skip, not a success: when this is the resolved main store, its
        # absence means sync points at the wrong root, not that all is well.
        reason = f"{db_path} does not exist on this peer"
        _SKIP_REASONS.append(reason)
        log.warning(
            f"  SKIPPED {db_path.stem}: {reason} — nothing replicated for it. "
            "inspect: m3 doctor (the resolved engine root), M3_SYNC_DBS, or pass --database PATH"
        )
        return None

    if dry_run:
        log.info(f"[DRY-RUN] Would run pg_sync.py --db {db_path}")
        return True

    log.info(f"Running pg_sync.py --db {db_path} ...")
    try:
        from _task_runtime import no_window_kwargs

        result = subprocess.run(
            [str(PY), str(BASE / "bin" / "pg_sync.py"), "--db", str(db_path)],
            capture_output=True,
            text=True,
            timeout=PG_SYNC_TIMEOUT,
            **no_window_kwargs(),
        )
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                log.info(f"  pg_sync[{db_path.stem}]: {line}")
        if result.returncode == _EXIT_SKIPPED:
            # ⚠ RAN IS NOT REPLICATED. pg_sync exits 75 (EX_TEMPFAIL) when it
            # could not take the sync lock and moved no rows. Treating that as
            # success is how this job reported "all systems synced" while the
            # local and warehouse stores drifted apart — the exact failure the
            # wrong-backend refusal further down was written to prevent. It is
            # not an ERROR either: the next hourly run picks it up.
            _SKIP_REASONS.append(f"pg_sync could not take the sync lock for {db_path} (exit {result.returncode})")
            log.warning(
                f"pg_sync SKIPPED {db_path.stem} — nothing was replicated.\n"
                f"  observed : pg_sync exited {result.returncode} (EX_TEMPFAIL); "
                f"its log above names the lock holder\n"
                f"  store    : {db_path}\n"
                f"  retry    : the next scheduled run; no data is lost\n"
                f"  inspect  : run `python bin/pg_sync.py --db {db_path}` by hand "
                f"to see the full reason"
            )
            return None  # neither success nor failure — see run_pg_sync()
        if result.returncode == 0:
            log.info(f"pg_sync completed for {db_path.stem}.")
            return True
        else:
            log.error(f"pg_sync exited with code {result.returncode} for {db_path.stem}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"pg_sync timed out after {PG_SYNC_TIMEOUT}s for {db_path.stem}")
        return False
    except Exception as e:
        log.error(f"pg_sync failed for {db_path.stem}: {type(e).__name__}: {e}")
        return False


# Why the FDW fast-path declined, for the refusal message downstream. Module
# state rather than a return value because _try_pg_fdw_fastpath's tri-state
# contract (True/False/None) is load-bearing at its call site and widening it to
# carry a reason would ripple further than this fix warrants.
_FDW_LAST_REASON: str = ""


def _set_fdw_reason(reason: str) -> None:
    """Record why the fast-path declined.

    A function rather than an inline `global`: two `global` statements in one
    function body is a SyntaxError, and the two fall-back handlers below both
    need to write this. Ruff does not catch that — only importing the module does.
    """
    global _FDW_LAST_REASON  # noqa: PLW0603 — deliberate module-level state
    _FDW_LAST_REASON = reason


def _try_pg_fdw_fastpath(dry_run: bool) -> "bool | None":
    """When the PRIMARY store is PostgreSQL, try the postgres_fdw fast-path
    (set-based server-side upserts) instead of the row-by-row SQLite<->PG bridge.

    Returns True/False on a completed fast-path run, or None to signal "not
    applicable / unavailable — fall back to the generic bridge" (SQLite/MariaDB
    primary, missing extension, unreachable warehouse). Never raises."""
    if _local_backend_name() != "postgres":
        return None  # SQLite/MariaDB primary -> generic bridge
    # Imported OUTSIDE the try below, whose except clause names
    # pg_fdw_sync.FdwUnavailable: a failed import there left the name unbound,
    # and evaluating that clause raised UnboundLocalError, which escaped this
    # "never raises" function and hid the import error.
    try:
        import pg_fdw_sync
    except Exception as e:  # noqa: BLE001 — reported, then the generic bridge runs
        _set_fdw_reason(f"FDW fast-path unavailable: pg_fdw_sync did not import ({type(e).__name__}: {e})")
        log.warning(
            f"FDW fast-path unavailable: pg_fdw_sync did not import "
            f"({type(e).__name__}: {e}) — falling back to generic bridge."
        )
        return None
    try:
        from m3_sdk import resolve_warehouse_dsn

        warehouse_dsn = resolve_warehouse_dsn()
        if not warehouse_dsn:
            # run_pg_sync() reports "not configured" as a skip before calling
            # this; reaching here means the DSN vanished between the two reads.
            _set_fdw_reason("FDW fast-path did not run: no warehouse DSN resolved (M3_CDW_PG_URL)")
            return None
        # ── The PRIMARY store, not the warehouse ──────────────────────────────
        # `ctx.pg_connection()` is the WAREHOUSE role by contract: it resolves
        # M3_CDW_PG_URL > PG_URL and deliberately does NOT read M3_PG_URL (see
        # M3Context.pg_connection's docstring). Using it here opened the
        # warehouse as `primary_conn`, so the FDW fast-path wired postgres_fdw
        # from the warehouse back to ITSELF and synced the warehouse with itself
        # — the primary store was never touched, and the run reported success.
        #
        # The primary must come from the primary role, whose invariant this
        # violated: the warehouse DSN never reaches the primary store.
        from m3_core.secret_store import PgRole, resolve_pg_dsn
        from memory.backends.postgres_backend import PostgresBackend

        primary_dsn = resolve_pg_dsn(PgRole.PRIMARY)
        if not primary_dsn:
            log.warning(
                "PG primary selected but no primary DSN resolved — store it: "
                "`m3 secret set M3_PRIMARY_PG_URL`. Falling back to the generic bridge."
            )
            return None

        # Watermarks stay LOCAL to this machine (per-peer cursors): a Mac, Linux
        # box, and PC each track their own push/pull against the shared CDW. On a
        # PG primary they live in the primary's own sync_watermarks table.
        #
        # ⚠ The same-database guard must be invoked EXPLICITLY here.
        # PostgresBackend.__init__ does `dsn or _resolve_dsn()`, so passing a DSN
        # BYPASSES _resolve_dsn — and with it _reject_same_as_warehouse. Relying
        # on construction to guard would silently re-permit the exact failure this
        # fix exists to close. Reuse the existing guard rather than writing a
        # second copy of the (host, port, dbname) comparison.
        from memory.backends.postgres_backend import _reject_same_as_warehouse

        try:
            _reject_same_as_warehouse(primary_dsn)
        except RuntimeError as e:
            log.error(f"Refusing the FDW fast-path: {e}")
            return False  # a real misconfiguration — do NOT fall through

        # try/finally: PostgresBackend owns a ThreadedConnectionPool, and this is a
        # short-lived cron process — leaking the pool would hold primary connections
        # open past the run on every hourly invocation.
        _primary_backend = PostgresBackend(dsn=primary_dsn)
        try:
            return _fdw_run(_primary_backend, pg_fdw_sync, warehouse_dsn, dry_run)
        finally:
            try:
                _primary_backend.close()
            except Exception:  # noqa: BLE001 — cleanup must not mask a real result
                pass
    except pg_fdw_sync.FdwUnavailable as e:
        _set_fdw_reason(f"FDW fast-path unavailable: {e}")
        log.info(f"FDW fast-path unavailable ({e}) — falling back to generic bridge.")
        return None
    except Exception as e:
        _set_fdw_reason(f"FDW fast-path errored: {type(e).__name__}: {e}")
        log.warning(f"FDW fast-path errored ({type(e).__name__}: {e}) — falling back to generic bridge.")
        return None


def _fdw_run(primary_backend, pg_fdw_sync, warehouse_dsn: str, dry_run: bool) -> bool:
    """Run the FDW sync against an already-resolved PRIMARY backend.

    Extracted from `_try_pg_fdw_fastpath` so the backend's connection pool can be
    closed in a `finally` there without wrapping the whole body in another level
    of try. Exceptions propagate: the caller owns the fall-back decision.
    """
    # Placeholders and the upsert clause come from the DIALECT, not hardcoded
    # `%s`. This block is PG-only *today* (the FDW fast-path requires it), but a
    # literal placeholder is a portability bug the moment a third backend lands
    # — and it costs nothing to ask the seam instead. `on_conflict_update`
    # renders identically on both current backends, which is the point: the call
    # site stops caring.
    dialect = primary_backend.dialect()
    p = dialect.param()
    upsert_clause = dialect.on_conflict_update("(direction)", ["last_synced_at"])

    with primary_backend.connection() as primary_conn:
        primary_conn.autocommit = False

        # No ad-hoc CREATE TABLE here: sync_watermarks is declared by pg_053 on
        # the PostgreSQL primary, which is the only store this path touches. The
        # DDL that used to live here was a second, independently-written copy of
        # pg_sync's — same table, different SQL, no owner — and a table created
        # by whichever code path ran first is how it ended up undeclared on PG
        # in the first place.
        def get_wm(direction: str):
            with primary_conn.cursor() as c:
                c.execute(f"SELECT last_synced_at FROM sync_watermarks WHERE direction={p}", (direction,))
                row = c.fetchone()
                return row[0] if row else None

        def set_wm(direction: str, ts: str):
            with primary_conn.cursor() as c:
                c.execute(
                    f"INSERT INTO sync_watermarks (direction, last_synced_at) VALUES ({p}, {p}) {upsert_clause}",
                    (direction, ts),
                )

        def warehouse_bound():
            # The warehouse's own clock for pull watermarks (pg_fdw_sync.arrival_bound),
            # over a short separate connection through the FIPS transport owner.
            from m3_core.pgconn import pg_connect

            wh = pg_connect(warehouse_dsn, connect_timeout=10)
            try:
                with wh.cursor() as c:
                    return pg_fdw_sync.arrival_bound(c)
            finally:
                wh.close()

        res = pg_fdw_sync.sync_pg_to_pg(
            primary_conn, warehouse_dsn, get_wm, set_wm, dry_run=dry_run, warehouse_bound=warehouse_bound
        )
        primary_conn.commit()
        log.info(f"PG->PG FDW fast-path completed: {res}")
        return True


def _local_backend_name() -> str:
    """The CONFIGURED backend of the local store (registry key db.backend) — the
    one place sync decides which path runs.

    The setting, not active_backend(): building a PostgreSQL backend with no DSN
    raises, and reading that as "sqlite" ran the SQLite bridge over a stale
    agent_memory.db on a PostgreSQL install and reported success. A bad setting
    raises ValueError, which propagates: a config error is not a backend.
    """
    from m3_core.paths import resolve_backend_name

    return resolve_backend_name()


def run_pg_sync(dry_run: bool) -> bool | None:
    """Run the warehouse sync. On a PostgreSQL primary, prefer the postgres_fdw
    fast-path; otherwise (or if it's unavailable) run the generic pg_sync.py
    bridge for each configured database. True if all succeed, False on any
    failure, None when skipped (reasons in _SKIP_REASONS)."""
    _SKIP_REASONS.clear()
    if _local_backend_name() == "postgres":
        from m3_sdk import resolve_warehouse_dsn

        if not resolve_warehouse_dsn():
            _SKIP_REASONS.append(
                "the primary store is PostgreSQL and no warehouse DSN is configured (fix: m3 secret set M3_CDW_PG_URL)"
            )
            log.warning(f"sync SKIPPED: {_SKIP_REASONS[-1]}")
            return None
    fast = _try_pg_fdw_fastpath(dry_run)
    if fast is not None:
        return fast

    # ── The generic bridge is SQLite-only on the LOCAL side ───────────────────
    # bin/pg_sync.py opens the local store with sqlite3.connect(). On a
    # PostgreSQL primary that either finds no file, or — worse — opens a stale or
    # empty agent_memory.db, reads zero rows, writes zero rows, and reports
    # SUCCESS. A replication path that silently moves nothing is the failure mode
    # this refusal exists to remove: an operator sees a green hourly job while
    # the two stores drift apart.
    #
    # Reaching here on a PG primary always means the FDW fast-path declined, so
    # name that reason too — otherwise the operator is told the bridge is
    # unsupported without learning that the supported path was one probe away.
    backend = _local_backend_name()
    if backend != "sqlite":
        log.error(
            f"Local store is {backend!r} (db.backend), but the generic sync "
            "bridge supports only a SQLite local store.\n"
            f"  {_FDW_LAST_REASON or 'FDW fast-path did not run.'}\n"
            "  Refusing to run rather than syncing an empty database.\n"
            "  Fix: ensure M3_PRIMARY_PG_URL points at the primary store and, for "
            "the fast path, ask an administrator for `CREATE EXTENSION postgres_fdw;` "
            "on it."
        )
        return False

    try:
        dbs = _resolve_dbs()
    except Exception as e:  # noqa: BLE001 — reported and failed, never guessed
        log.error(
            f"could not resolve the database to sync ({type(e).__name__}: {e}); nothing was replicated. "
            "inspect: m3 doctor, or pin one with --database PATH / M3_SYNC_DBS"
        )
        return False
    log.info(f"pg_sync target DBs: {[str(d) for d in dbs]}")
    results = []
    for db in dbs:
        ok = run_pg_sync_for_db(db, dry_run)
        results.append(ok)
    # THREE outcomes, not two. `None` means "ran, replicated nothing" (the lock
    # was unavailable). Folding it into True reported a skip as a completed
    # sync; folding it into False would make a routine hourly no-op look like a
    # fault. `all()` over a list containing None is False, which is why this is
    # spelled out rather than left to truthiness.
    if any(r is False for r in results):
        return False
    return None if any(r is None for r in results) else True


# ── main ──────────────────────────────────────────────────────────────────────


def main():
    # Configured here, not at import: other m3 code imports this module, and an
    # import-time basicConfig turned on INFO logging for the whole process
    # (every `m3 memory` command printed each HTTP request).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser(description="Hourly sync runner")
    parser.add_argument("--dry-run", action="store_true", help="Check connectivity only")
    sys.path.insert(0, str(BASE / "bin"))
    from _task_runtime import add_log_file_arg, setup_task_runtime
    from m3_sdk import add_database_arg

    add_log_file_arg(parser)
    add_database_arg(parser)
    args = parser.parse_args()

    setup_task_runtime(args.log_file or LOG_FILE, lock_name="sync_all")

    if args.database:
        # Pass-through env so the pg_sync subprocess inherits.
        pin_database(args.database)

    # sys.platform, not platform.system() (WMI-hang risk on Py3.14/Windows).
    _os = os_name()
    log.info(f"=== sync_all starting [{_os}] ===")

    target = warehouse_target()
    if target is None:
        log.info("No warehouse configured (M3_POSTGRES_SERVER, M3_CDW_PG_URL or a stored PG_URL) — skipping sync.")
        sys.exit(0)
    host, port, source = target

    if not is_reachable(host, port):
        log.warning(
            f"PostgreSQL data warehouse ({host}:{port}, from {source}) "
            f"unreachable — skipping sync until the next scheduled run."
        )
        sys.exit(0)

    log.info(f"PostgreSQL data warehouse ({host}:{port}, from {source}) reachable — running full sync.")

    pg_ok = run_pg_sync(args.dry_run)

    if pg_ok is None:
        # Ran cleanly, replicated nothing. Say that, and exit with the same
        # EX_TEMPFAIL pg_sync used so a wrapping cron/CI step can tell a no-op
        # from a completed sync without parsing the log.
        reasons = "; ".join(_SKIP_REASONS) or "reason not recorded (see the log above)"
        log.warning(
            "=== sync_all complete: SKIPPED — nothing replicated ===\n"
            f"  observed : {reasons}\n"
            f"  inspect  : {LOG_FILE} for the details of each"
        )
        sys.exit(_EXIT_SKIPPED)
    if pg_ok:
        log.info("=== sync_all complete: all systems synced ===")
        sys.exit(0)
    else:
        log.error(f"=== sync_all finished with errors: pg={pg_ok} ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
