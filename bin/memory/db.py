"""SQLite primitives, schema lifecycle, history, and gate cache.

Phase 2.B of the memory_core modularization. Holds:
  - Connection helpers (_db, _conn) that route through the active
    M3Context's connection pool
  - Lazy schema-init (_lazy_init, _ensure_sync_tables, _backfill_change_agent)
    with per-DB-path tracking via _initialized_dbs
  - History audit trail (_record_history, memory_history_impl)
  - Gate cache (_gate_active, _gate_count_query, _GATE_CACHE, _GATE_CACHE_TTL)
    used by the auto-activation gates in memory_core
  - Access-stamp batcher (_access_stamp_flusher, _enqueue_access_stamps,
    _access_pending, _access_lock, _access_flusher_task)

Mutable module-level state (`_initialized_dbs`, `_GATE_CACHE`,
`_access_pending`, `_access_flusher_task`) is externally observable
through the memory_core re-export shim. Callers must NOT rebind these
names from outside; only mutate the existing container objects.

Subtle dependency notes:
  - `_db()` resolves the active context via `M3Context.for_db(resolve_db_path(None))`
    rather than calling memory_core's `_current_ctx` (avoids the circular
    import that would otherwise loop db.py back through memory_core).
  - `_backfill_change_agent` uses `infer_change_agent` from `m3_sdk` directly,
    again to avoid the circular.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from embedding_utils import infer_change_agent as _infer_change_agent_util
from m3_core.config import settings as _m3cfg
from m3_core.errors import log_swallowed
from m3_sdk import M3Context, migration_lock, resolve_db_path

from . import config

logger = logging.getLogger("memory.db")


# ──────────────────────────────────────────────────────────────────────────────
# Connection-pool state (per-thread)
# ──────────────────────────────────────────────────────────────────────────────
_local = threading.local()
_init_lock = threading.RLock()
_initialized = False  # legacy flag — once true, stays true
# Per-DB-path tracking. Externally imported (per the migration audit) — DO NOT
# rebind this set; only mutate it in place.
_initialized_dbs: set[str] = set()


# ──────────────────────────────────────────────────────────────────────────────
# Gate cache: ~5 min memoized COUNT(*) results for auto-activation gates
# ──────────────────────────────────────────────────────────────────────────────
# Externally imported. DO NOT rebind; only mutate.
_GATE_CACHE: dict[str, tuple[bool, float]] = {}
_GATE_CACHE_TTL = 300  # seconds; counts can change as drains run

_OBS_COUNT_QUERY = "SELECT COUNT(*) FROM memory_items WHERE type='observation' AND COALESCE(is_deleted,0)=0"
_ENTITY_COUNT_QUERY = "SELECT COUNT(*) FROM entities"


# ──────────────────────────────────────────────────────────────────────────────
# Access-stamp batcher: coalesce last_accessed_at UPDATEs into bulk writes
# ──────────────────────────────────────────────────────────────────────────────
_ACCESS_FLUSH_INTERVAL = 0.25  # seconds
_access_pending: set[str] = set()
_access_flusher_task: asyncio.Task | None = None
_access_lock = asyncio.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Connection helpers
# ──────────────────────────────────────────────────────────────────────────────
def _current_ctx_local() -> M3Context:
    """Return the M3Context for the currently active DB path.

    Resolved here instead of imported from memory_core to avoid a circular
    import. Honors active_database() ContextVar > M3_DATABASE env > default.
    """
    return M3Context.for_db(resolve_db_path(None))


@contextmanager
def _db():
    """Open a read/write connection from the ACTIVE backend.

    On SQLite (the default) this opens a pooled ``sqlite3.Connection`` from the
    active context's pool, triggering ``_lazy_init`` on first touch per DB path,
    committing on clean exit and rolling back on exception — byte-identical to
    the pre-backend behavior.

    On PostgreSQL it delegates to ``active_backend().connection()``, whose
    context manager already applies the SAME discipline (commit on clean exit,
    rollback on exception, return to pool). We therefore just yield through its
    already-wrapped connection rather than re-implementing commit/rollback here.

    Routing lives in this single canonical helper so every module that did
    ``from .db import _db`` becomes backend-aware for free; the previous per-file
    wrapper (memory.write._db) collapses into this. Tests that monkeypatch a
    module-local ``_db`` still win — they replace the imported name, not this
    definition — and all such tests run on the SQLite default, where this path is
    unchanged.
    """
    # Backend routing. SQLite is the legacy special case (the M3Context pool path
    # below); EVERY OTHER backend routes through the seam's connection(). Keyed on
    # `!= "sqlite"`, not `== "postgres"`, so a future SQL backend (e.g. MariaDB)
    # goes through the seam too rather than silently falling into the SQLite path
    # and touching the wrong store. The PROBE is guarded so a missing/half-
    # initialized backend layer falls through to the historical SQLite path; but
    # once we route to the seam, the connection and the caller's work run OUTSIDE
    # the guard — a real backend error must propagate, never be swallowed into a
    # silent fall-through to the wrong store.
    # Only ImportError falls through to the SQLite path (the bootstrap case). A
    # seam that REFUSES its configuration (PostgreSQL with no DSN) propagates:
    # this caught Exception until 2026-10-10, so such an install silently read
    # and wrote a local SQLite file -- the same defect fixed in seam_backend.
    _use_seam = False
    _backend = None
    try:
        from memory.backends import active_backend as _ab
    except ImportError:
        _ab = None
    if _ab is not None:
        _backend = _ab()
        _use_seam = not _backend.stores_databases_as_files

    if _use_seam:
        # The backend's connection() applies its own commit/rollback discipline
        # (see StorageBackend.connection); we only yield through it. Errors propagate.
        with _backend.connection() as conn:
            _cohesion_once(conn, _backend)
            yield conn
        return

    active_ctx = _current_ctx_local()
    if os.environ.get("M3_DEBUG"):
        print(f"DEBUG DB PATH: {active_ctx.db_path}")
    _lazy_init(active_ctx.db_path)
    with active_ctx.get_sqlite_conn() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


_COHESION_CHECKED: set = set()


def _cohesion_once(conn, backend) -> None:
    """Salt cohesion for a server backend, once per process per backend (the
    SQLite path runs it when its pool is built)."""
    key = id(backend)
    if key in _COHESION_CHECKED:
        return
    _COHESION_CHECKED.add(key)
    from m3_core.context import verify_salt_cohesion

    verify_salt_cohesion(conn, f"the {backend.name} store")


@contextmanager
def _conn():
    """Legacy alias for _db context manager (C7)."""
    with _db() as db:
        yield db


# Monotonic savepoint namer — SAVEPOINT names must be unique when nested; a plain
# module counter suffices (single-process; never persisted).
_savepoint_seq = 0


@contextmanager
def savepoint(conn):
    """Isolate a block's failure to a SAVEPOINT: on ANY exception, roll the
    savepoint back (leaving the outer transaction USABLE) and re-raise; on success,
    release it.

    Unlike :func:`tolerant_schema`, this swallows NOTHING — it exists to make an
    EXISTING ``try/except`` around ``db.execute`` PG-safe. The historical
    dual-path idiom ``try: <query with pinned> except missing-schema: <query
    without pinned>`` works on SQLite but breaks on PG (the first query's error
    aborts the txn before the fallback runs). Wrapping just the first attempt in
    ``savepoint`` lets the fallback run on a healthy connection:

        try:
            with savepoint(db):
                res = db.execute("... COALESCE(pinned,0)=0 ...")
        except Exception as e:
            if not _is_missing_schema(e): raise
            res = db.execute("... without pinned ...")   # runs on a clean txn

    Standard SQL (SAVEPOINT/ROLLBACK TO/RELEASE), identical on SQLite and psycopg2.
    """
    global _savepoint_seq
    _savepoint_seq += 1
    sp = f"m3_sp_{_savepoint_seq}"
    conn.execute(f"SAVEPOINT {sp}")
    try:
        yield
    except Exception:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:  # noqa: BLE001 — savepoint cleanup while re-raising the original error
            pass
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {sp}")


@contextmanager
def tolerant_schema(conn, *, reraise_real=True):
    """Run a block of statements that MAY hit a not-yet-migrated schema (missing
    column/table), tolerating exactly that class of error and rolling back ONLY
    that block — never the whole transaction.

    WHY THIS EXISTS. The historical idiom was ``try: db.execute(...) except
    OperationalError: pass`` — fine on SQLite, but BROKEN on PostgreSQL: a caught
    error still ABORTS the surrounding transaction, so every later statement fails
    with ``InFailedSqlTransaction``. Wrapping the block in a SAVEPOINT makes the
    tolerance real on BOTH backends: on a caught missing-schema error we
    ``ROLLBACK TO SAVEPOINT`` (undoing only this block) and continue; on success we
    ``RELEASE`` it. SAVEPOINT / ROLLBACK TO / RELEASE are standard SQL supported
    identically by SQLite and psycopg2, so this is backend-uniform — no dialect
    branch.

    Usage (replaces the try/except-missing-schema idiom):

        with tolerant_schema(db):
            db.execute("UPDATE memory_items SET confidence = ... WHERE ...")

    A missing-column/table error inside the block is swallowed (the pass degrades
    to a no-op for that statement). Any OTHER error rolls the savepoint back and
    re-raises (``reraise_real=True``), so real bugs still fail loud (§3).

    Classification of "tolerable" goes through the seam
    (``dialect().is_undefined_object_error``), so it is correct on both backends
    (SQLite message text vs PG SQLSTATE 42703/42P01)."""
    global _savepoint_seq
    _savepoint_seq += 1
    sp = f"m3_tol_{_savepoint_seq}"
    try:
        from memory.backends import dialect as _dialect

        _is_undef = _dialect().is_undefined_object_error
    except Exception:
        # No seam (raw sqlite fixture): fall back to a message match so the
        # tolerance still works in tests that pass a bare sqlite3 connection.
        def _is_undef(exc):
            m = str(exc).lower()
            return "no such column" in m or "no such table" in m or "no column named" in m

    conn.execute(f"SAVEPOINT {sp}")
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — classified below
        # Roll back just this block so the outer transaction stays usable.
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            conn.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception:  # noqa: BLE001 — savepoint cleanup; the original error is handled below
            pass
        if reraise_real and not _is_undef(exc):
            raise  # a genuine error — do not swallow it
        return
    # Clean block → release the savepoint (keep its effects).
    conn.execute(f"RELEASE SAVEPOINT {sp}")


# ──────────────────────────────────────────────────────────────────────────────
# Schema lifecycle
# ──────────────────────────────────────────────────────────────────────────────
def _ensure_sync_tables(db_path: str | None = None) -> None:
    """Run pending migrations against the active DB.

    Fast path: if the schema is already at the latest version on disk
    (compared against the migration files in memory/migrations/ or
    memory/chatlog_migrations/), skip the subprocess entirely.

    Test escape hatch: when M3_SKIP_MIGRATIONS=1 is set, return without
    doing anything. Test fixtures that pre-create the full post-v031
    schema (via tests/conftest.py's `create_full_main_schema`) set this
    so the migration subprocess doesn't re-run on an already-current DB.
    """
    if os.environ.get("M3_SKIP_MIGRATIONS", "").lower() in ("1", "true", "yes"):
        return
    # SQLite ONLY. Everything below is the SQLite migration chain: it reads
    # memory/migrations/*.up.sql, probes sqlite_master for schema_versions, and
    # shells out to bin/migrate_memory.py, which has no PostgreSQL path at all.
    # PostgreSQL gets its schema from its own pg_* migrations via
    # backend.ensure_schema().
    #
    # Gated on the capability, not on `!= "postgres"` (§10a) -- and gated rather
    # than converted, because there is no backend-neutral meaning for "compare
    # the DB's applied version against the .up.sql files on disk". Before this
    # gate the sqlite_master probe below simply raised on PG and was swallowed
    # by the surrounding try, so the "fast path" never fired and every call
    # shelled out to a migration runner that could not help it.
    try:
        from memory.backends import resolve_backend_name

        if resolve_backend_name() != "sqlite":
            return
    except ImportError:  # no seam yet (bootstrap) => SQLite
        pass
    try:
        migration_script = str(Path(config.BASE_DIR) / "bin" / "migrate_memory.py")

        # Detect chatlog context via schema fingerprint.
        active = db_path or resolve_db_path(None)
        target_flag: list[str] = []
        target_kind = "main"
        try:
            sys.path.insert(0, str(Path(config.BASE_DIR) / "bin"))
            from migrate_memory import _classify_db

            if _classify_db(active) == "chatlog":
                target_flag = ["--target", "chatlog"]
                target_kind = "chatlog"
        except Exception:  # noqa: BLE001 — store classification for --target; default target on failure
            pass

        # Fast path: compare DB's applied version vs. the highest .up.sql file
        # number for the resolved target. If equal, no migrations to apply.
        try:
            mig_dir = (
                Path(config.BASE_DIR) / "memory" / ("chatlog_migrations" if target_kind == "chatlog" else "migrations")
            )
            file_versions = []
            pattern = re.compile(r"^(\d+)_.*\.up\.sql$")
            for entry in mig_dir.iterdir():
                m = pattern.match(entry.name)
                if m:
                    file_versions.append(int(m.group(1)))
            latest_on_disk = max(file_versions) if file_versions else -1

            db_latest = -1
            conn = sqlite3.connect(f"file:{active}?mode=ro", uri=True, timeout=2.0)
            try:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('schema_versions','schema_migrations')"
                ).fetchall()
                tables = {r[0] for r in cur}
                if "schema_versions" in tables:
                    row = conn.execute(
                        "SELECT MAX(CAST(version AS INTEGER)) FROM schema_versions "
                        "WHERE typeof(CAST(version AS INTEGER))='integer' "
                        "  AND CAST(version AS INTEGER) > 0"
                    ).fetchone()
                    db_latest = int(row[0]) if row and row[0] is not None else -1
                elif "schema_migrations" in tables:
                    row = conn.execute(
                        "SELECT MAX(CAST(version AS INTEGER)) FROM schema_migrations "
                        "WHERE typeof(CAST(version AS INTEGER))='integer' "
                        "  AND CAST(version AS INTEGER) > 0"
                    ).fetchone()
                    db_latest = int(row[0]) if row and row[0] is not None else -1
            finally:
                conn.close()

            if latest_on_disk >= 0 and db_latest >= latest_on_disk:
                return
        except Exception as le:  # noqa: BLE001 — logged; the caller carries on
            logger.warning(
                f"migration up-to-date check failed; observed: {type(le).__name__}: {le}; running the migrator anyway. inspect: python bin/migrate_memory.py status"
            )

        env = os.environ.copy()
        if target_flag:
            env["M3_DATABASE"] = active
        # ⚠ RESOLVE THE HELPER, DO NOT HOPE FOR IT.
        #
        # `from _task_runtime import ...` is a BARE module name: it resolves
        # only when bin/ is already on sys.path. A scheduled task runs
        # `python bin/memory_maintenance.py`, which puts bin/ on the path as the
        # script's own directory — but an import of memory.db from anywhere
        # else does not, and the except-branch then set _nw = {} SILENTLY. The
        # migration child was spawned with no CREATE_NO_WINDOW, so it flashed a
        # console on a user's desktop at 03:00 (reported 2026-09-20).
        #
        # Belt and braces: add bin/ to sys.path before retrying, and if the
        # helper is still unreachable, synthesise the flag rather than
        # degrading to "no flag" — the whole point is that a background spawn
        # must never allocate a console.
        try:
            from _task_runtime import no_window_kwargs

            _nw = no_window_kwargs()
        except ImportError:
            _bin_dir = str(Path(config.BASE_DIR) / "bin")
            if _bin_dir not in sys.path:
                sys.path.insert(0, _bin_dir)
            try:
                from _task_runtime import no_window_kwargs

                _nw = no_window_kwargs()
            except ImportError:
                logger.debug("_task_runtime unavailable; applying CREATE_NO_WINDOW directly")
                _flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                _nw = {"creationflags": _flag} if _flag else {}
        with migration_lock():
            # ⚠ CAPTURE THE CHILD'S STDOUT, THEN RELAY IT TO STDERR.
            #
            # migrate_memory is also a human-facing CLI, so it prints its plan
            # ("Current version: v000 / Will apply: ...") to stdout. Inheriting
            # that handle injected those lines into the STDOUT OF WHATEVER
            # COMMAND happened to trigger the auto-migration — so the first
            # `m3 ... --as_records` against a fresh database emitted migration
            # prose followed by JSON, and every parser downstream failed.
            #
            # Now that every tool is pipeable, "stdout is the result, stderr is
            # the narration" is a contract, not a preference (§3). Relayed
            # rather than discarded: a migration is worth seeing, just not in
            # the data channel.
            proc = subprocess.run(
                [sys.executable, migration_script, "up", "--yes", *target_flag],
                check=True,
                timeout=300,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                **_nw,
            )
            for stream in (proc.stdout, proc.stderr):
                if stream and stream.strip():
                    print(stream.rstrip(), file=sys.stderr)
    except subprocess.CalledProcessError as e:
        # §3: capturing output must not HIDE a failure. Without this the child's
        # diagnostics died inside the CalledProcessError and the log carried
        # only an exit code.
        for stream in (e.stdout, e.stderr):
            if stream and str(stream).strip():
                print(str(stream).rstrip(), file=sys.stderr)
        logger.exception(f"_ensure_sync_tables migration failed: {e}")
    except Exception as e:
        logger.exception(f"_ensure_sync_tables failed: {e}")


def _backfill_change_agent() -> None:
    from memory.backends import dialect

    _p = dialect().param()
    try:
        with _db() as db:
            rows = db.execute("SELECT id, agent_id, model_id FROM memory_items WHERE change_agent IS NULL").fetchall()
            for row in rows:
                agent = _infer_change_agent_util(row["agent_id"] or "", row["model_id"] or "", default="legacy")
                db.execute(f"UPDATE memory_items SET change_agent = {_p} WHERE id = {_p}", (agent, row["id"]))
    except Exception as e:
        logger.warning(f"Backfill failed: {e}")


def ensure_pinned_column(conn) -> None:
    """Idempotently add memory_items.pinned INTEGER DEFAULT 0. Best-effort:
    a DB that predates this (or already has it) is a no-op. Mirrors the
    bin/enrich/prep.py::_ensure_migration_025 runtime-DDL fallback — no
    migration file is required (the migrations dir chain is not fully
    in-tree; pre-v2026.7.1.0 DBs are no longer supported).

    Pinned memories (pinned=1) are exempt from decay, expiry, and
    retention purges — see bin/memory_maintenance.py.
    """
    try:
        # Portable column check: PRAGMA is SQLite-only and, on PostgreSQL, its
        # failure ABORTS the psycopg2 transaction (poisoning every later query on
        # the connection) even though the except below swallows the error. Use the
        # dialect's metadata query — pragma_table_info on SQLite,
        # information_schema.columns on PG — which never poisons the txn.
        try:
            from memory.backends import dialect as _dialect

            _sql, _params = _dialect().columns_of("memory_items")
            cols = {r[0] for r in conn.execute(_sql, _params).fetchall()}
        except Exception:
            # No backend seam (raw sqlite fixture): PRAGMA is safe on sqlite.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
        if "pinned" not in cols:
            conn.execute("ALTER TABLE memory_items ADD COLUMN pinned INTEGER DEFAULT 0")
            conn.commit()
    except Exception:  # noqa: BLE001 — never break the caller
        pass


def _lazy_init(db_path: str | None = None) -> None:
    """Run one-time schema + backfill per DB path. Per-DB to support multi-DB."""
    global _initialized
    key = db_path or resolve_db_path(None)
    with _init_lock:
        if key in _initialized_dbs:
            return
        _initialized_dbs.add(key)
        _initialized = True  # legacy flag — once true, stays true

        # A DISPATCH store is never main-initialised. It is bootstrapped from
        # its own migrations (memory/dispatch_migrations) and carries neither
        # main nor chatlog schema, so every routine below is wrong for it:
        # _ensure_sync_tables would hand it to the MAIN migration runner, which
        # creates main tables in it, and _backfill_change_agent queries
        # memory_items, which it does not have.
        #
        # Guarded HERE, at the convergence point, rather than in each routine
        # (§2): N callers that must all satisfy a rule get ONE check where they
        # meet, so a future routine added to this function inherits it instead
        # of needing to remember.
        #
        # Measured while building the store split: without this, a bootstrapped
        # agent_dispatch.db was classified "unknown", migrated as a main store,
        # and its notification_dispatch table was replaced -- which surfaced as
        # an EMPTY INBOX rather than an error.
        try:
            from migrate_memory import _classify_db as _classify

            if _classify(key) == "dispatch":
                return
        except Exception:  # noqa: BLE001 — classifier unavailable during
            # bootstrap; fall through to the historical behaviour rather than
            # skipping initialisation of a store that may genuinely need it.
            pass

        try:
            _ensure_sync_tables(key)
            _backfill_change_agent()
            try:
                # DELIBERATELY a raw connection, not backend.connection().
                # We are INSIDE _lazy_init, holding _init_lock; SqliteBackend
                # .connection() delegates straight back to _db(), which re-enters
                # _lazy_init -> deadlock on that same lock. This bootstrap must
                # open its own handle by construction.
                #
                # It is also correct to be SQLite-shaped here: PostgreSQL gets
                # the pinned column from its own pg_* migration chain via
                # ensure_schema(), so this path is the SQLite one. Gated so it
                # does not open a stale file on PG.
                from memory.backends import resolve_backend_name

                if resolve_backend_name() == "sqlite":
                    conn = sqlite3.connect(str(key), timeout=10.0)
                    try:
                        ensure_pinned_column(conn)
                    finally:
                        conn.close()
            except Exception:  # noqa: BLE001 — documented best-effort bootstrap (see comment)
                # Best-effort — never let the pinned-column bootstrap break
                # DB init. ensure_pinned_column() also self-guards, but the
                # connect() itself could fail on an exotic path.
                pass
        except Exception:
            # Do not trap init in a permanently-failed state — let next caller retry.
            _initialized_dbs.discard(key)
            raise


# ──────────────────────────────────────────────────────────────────────────────
# History audit trail
# ──────────────────────────────────────────────────────────────────────────────
def _record_history(
    memory_id: str,
    event: str,
    prev_value: str | None = None,
    new_value: str | None = None,
    field: str = "content",
    actor_id: str = "",
    db: Any = None,
) -> None:
    """Records a change event in the memory_history audit trail.

    Pass ``db`` when the caller already holds an open connection (e.g. inside a
    ``with _db() as db:`` block). Opening a second pool connection while the
    outer one has an uncommitted writer causes SQLite WAL writer contention,
    which burns the full ``busy_timeout`` per call.
    """
    from memory.backends import dialect

    _d = dialect()
    row = (str(uuid.uuid4()), memory_id, event, prev_value, new_value, field, actor_id)
    sql = (
        "INSERT INTO memory_history "
        "(id, memory_id, event, prev_value, new_value, field, actor_id) "
        f"VALUES ({_d.placeholder(7)})"
    )
    try:
        if db is not None:
            db.execute(sql, row)
        else:
            with _db() as inner:
                inner.execute(sql, row)
    except Exception as e:
        # NOTE: on PostgreSQL a failed statement aborts the WHOLE transaction, so
        # a swallowed error here on the caller's shared `db` would silently roll
        # back the caller's writes too. Dialecting the SQL above prevents the
        # syntax error that used to trigger exactly that (superseded rows silently
        # not persisting). Keep the catch for genuinely-tolerable failures (e.g. a
        # pre-migration DB with no memory_history table).
        log_swallowed(logger, e, f"History recording failed: {e}")


def memory_history_impl(memory_id: str, limit: int = 20, as_records: bool = False) -> str:
    """Returns the change history for a memory item."""
    # Local import: memory.records is leaf-level, but db.py sits low in the
    # import graph and a module-scope import here risks a cycle for no gain.
    from memory import records
    from memory.backends import dialect

    _p = dialect().param()
    with _db() as db:
        rows = db.execute(
            "SELECT event, field, prev_value, new_value, actor_id, created_at "
            f"FROM memory_history WHERE memory_id = {_p} ORDER BY created_at DESC LIMIT {_p}",
            (memory_id, limit),
        ).fetchall()
    if not rows:
        return records.emit(
            f"No history found for {memory_id}", records.as_records_payload((), memory_id=memory_id), as_records
        )
    lines = [f"History for {memory_id} ({len(rows)} events):"]
    for r in rows:
        prev = (r["prev_value"] or "")[:80]
        new = (r["new_value"] or "")[:80]
        lines.append(
            f"  [{r['created_at']}] {r['event']} ({r['field']}) by {r['actor_id'] or 'unknown'}: {prev!r} -> {new!r}"
        )
    # Records carry prev_value/new_value IN FULL. The display truncates both to
    # 80 chars for width, so a caller diffing an audit trail from the string got
    # silently clipped values -- the exact fallback this param removes.
    return records.emit("\n".join(lines), records.as_records_payload(rows, memory_id=memory_id), as_records)


# ──────────────────────────────────────────────────────────────────────────────
# Gate cache
# ──────────────────────────────────────────────────────────────────────────────
_GATE_QUERY_WARNED: set[str] = set()


def _gate_count_query(query: str) -> int:
    """Run a COUNT(*) query against the active DB. Returns 0 on error, which
    turns its gate off, so the first failure of each query is warned."""
    try:
        with _db() as db:
            row = db.execute(query).fetchone()
            if row is None:
                return 0
            return int(row[0])
    except Exception as e:  # noqa: BLE001 — the gate stays off; warned once per query
        if query not in _GATE_QUERY_WARNED:
            _GATE_QUERY_WARNED.add(query)
            logger.warning(
                f"gate count failed, gate treated as off; observed: "
                f"{type(e).__name__}: {e}. query: {query[:120]}. inspect: m3 doctor"
            )
        return 0


def _gate_active(env_var: str, count_query: str, threshold: int = 1) -> bool:
    """True if env var is explicitly on, or auto-activated by data presence.

    Cached per (env_var, count_query) for ~5 min; the cache is invalidated by
    process restart or natural TTL expiry. Single-process; no thread lock — a
    stampede on first miss would just run COUNT(*) twice, harmless.

    The count-query function is resolved at call time so tests that
    monkeypatch `memory_core._gate_count_query` (legacy pattern) take
    effect. Production reads the module-local `_gate_count_query`.
    """
    if os.environ.get(env_var, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if _m3cfg.setting("memory.disable_auto_activation"):
        return False
    cache_key = f"{env_var}::{count_query}"
    cached = _GATE_CACHE.get(cache_key)
    now = time.monotonic()
    # Resolve TTL via memory_core for legacy tests that monkeypatch it
    # (test_phase_l_auto_activation::test_cache_expires_after_ttl).
    _ttl = _GATE_CACHE_TTL
    _count_fn = _gate_count_query
    try:
        import memory_core as _mc

        _ttl = getattr(_mc, "_GATE_CACHE_TTL", _ttl)
        _count_fn = getattr(_mc, "_gate_count_query", _count_fn)
    except ImportError:
        pass
    if cached is not None and (now - cached[1]) < _ttl:
        return cached[0]
    count = _count_fn(count_query)
    active = count >= threshold
    _GATE_CACHE[cache_key] = (active, now)
    return active


# ──────────────────────────────────────────────────────────────────────────────
# Access-stamp batcher
# ──────────────────────────────────────────────────────────────────────────────
def _flush_access_batch(batch: list) -> int:
    """Write one batch of access stamps. Returns the number of ids attempted.

    ⚠ THROUGH THE SEAM. This carried literal `?` placeholders, which PostgreSQL
    rejects outright — and the caller swallowed the error at debug level, so on
    PG `last_accessed_at` was NEVER written and `access_count` never moved.
    Everything downstream of those two columns was therefore dead on that
    backend: decay reinforcement had nothing to read, and `memory_grade`'s
    retrieval window could never open, so grading rejected every verdict as
    stale. Verified against a live cluster (§0.4, §10a).

    Extracted so the flusher loop and the synchronous final drain share ONE
    implementation rather than two copies of the same UPDATE.
    """
    if not batch:
        return 0
    from memory.backends import dialect as _dialect

    _d = _dialect()
    _p = _d.param()
    now_iso = datetime.now(UTC).isoformat()
    try:
        with _db() as db:
            placeholders = ",".join([_p] * len(batch))
            db.execute(
                f"UPDATE memory_items "
                f"SET last_accessed_at = {_p}, access_count = "
                f"COALESCE(access_count, 0) + 1 "
                f"WHERE id IN ({placeholders})",
                (now_iso, *batch),
            )
            if hasattr(db, "commit"):
                db.commit()
    except Exception as e:  # noqa: BLE001 — never break a read path
        log_swallowed(logger, e, f"access-stamp flush failed (batch={len(batch)}): {e}")
        return 0
    return len(batch)


def flush_access_stamps_now() -> int:
    """Drain pending access stamps SYNCHRONOUSLY. Returns ids written.

    ⚠ WHY A SHORT-LIVED PROCESS NEEDS THIS. The batcher is a 0.25s async task
    that lives for the lifetime of an event loop. The MCP server has one; a CLI
    invocation does not — it exits long before the first tick, so a search run
    from the shell stamped NOTHING.

    That made `memory_grade` unreachable over a pipe: the grade window is
    authorised by `last_accessed_at`, so every verdict came back
    `outside_feedback_window` no matter how promptly it was sent. Now that every
    tool is pipeable, `m3 memory memory_search ... | ... | m3 memory
    memory_grade` is an obvious thing to write, and it could not work.

    Safe to call when nothing is pending (returns 0) and from any thread.
    """
    global _access_pending
    if not _access_pending:
        return 0
    batch = list(_access_pending)
    _access_pending.clear()
    return _flush_access_batch(batch)


async def _access_stamp_flusher() -> None:
    """Drains _access_pending into a single batched UPDATE on a fixed cadence.

    Lives for the lifetime of the running event loop. Per-loop singleton —
    created lazily by ``_enqueue_access_stamps``. Catches its own errors so a
    transient DB lock can't kill the long-lived task.
    """
    while True:
        try:
            await asyncio.sleep(_ACCESS_FLUSH_INTERVAL)
            async with _access_lock:
                if not _access_pending:
                    continue
                batch = list(_access_pending)
                _access_pending.clear()
            _flush_access_batch(batch)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001 — keep the task alive
            log_swallowed(logger, e, f"access-stamp flusher recoverable error: {e}")


def _enqueue_access_stamps(ids) -> None:
    """Buffer hit-ids for a fire-and-forget UPDATE. Idempotent / dedup'd."""
    global _access_flusher_task
    if not ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop -> skip; sync callers don't need this
    _access_pending.update(i for i in ids if i)
    if _access_flusher_task is None or _access_flusher_task.done():
        _access_flusher_task = loop.create_task(_access_stamp_flusher())


# ──────────────────────────────────────────────────────────────────────────────
# Read-access audit log (GDPR Art. 30: who read what, when; user decision
# 2026-10-11: always on, 90-day retention). memory_access_log, migration 053/pg_062.
# ──────────────────────────────────────────────────────────────────────────────
# Its own batcher, flushed by a DAEMON THREAD, not the access-stamp batcher above:
# that one runs only inside an event loop, and memory_get is synchronous, so an
# audit log riding it would silently miss every direct get. Each record carries
# the caller (m3_core.caller, bound by the dispatcher) and the STORE it was read
# from: the flush thread does not inherit the reader's active_database
# ContextVar, so without it a read under --store X would be logged into the
# default store.
_READ_LOG_FLUSH_S = 0.25
_read_log_pending: list[tuple[str, str, str, str | None, str]] = []  # (store, memory, agent, user, op)
_read_log_lock = threading.Lock()
_read_log_thread: threading.Thread | None = None


def record_reads(memory_ids, op: str, user_id: str | None = None) -> None:
    """Queue one audit row per memory read (op: get | search_hit | export).

    Never raises into the read path and never blocks it: a failure to record is
    logged by the flusher. Off only when audit.read_log is set false."""
    global _read_log_thread
    ids = [m for m in (memory_ids or ()) if m]
    if not ids:
        return
    try:
        if not _m3cfg.setting("audit.read_log"):
            return
        from m3_core.caller import current_caller

        _bind_read_log_deps()
        store = str(resolve_db_path(None))
        agent = current_caller()
        with _read_log_lock:
            _read_log_pending.extend((store, m, agent, user_id, op) for m in ids)
            if _read_log_thread is None or not _read_log_thread.is_alive():
                _read_log_thread = threading.Thread(target=_read_log_loop, name="m3-read-log", daemon=True)
                _read_log_thread.start()
    except Exception as e:  # noqa: BLE001 — auditing must not break a read
        log_swallowed(logger, e, f"read-access log: could not queue {len(ids)} read(s): {e}")


_read_log_deps: tuple[Any, Any] | None = None  # (active_database, dialect)


def _bind_read_log_deps() -> tuple[Any, Any]:
    """Resolve the flusher's imports on the CALLER's thread, once. The flush
    thread must never import: a lazy `memory.backends` import there races any
    concurrent rebuild of the `memory` package (measured: KeyError
    'memory.chunking' killed the thread under the test suite's namespace purge),
    and a dead flusher is a silent audit gap."""
    global _read_log_deps
    if _read_log_deps is None:
        from m3_core.paths import active_database

        from memory.backends import dialect

        _read_log_deps = (active_database, dialect)
    return _read_log_deps


def _read_log_loop() -> None:
    while True:
        time.sleep(_READ_LOG_FLUSH_S)
        try:
            flush_read_log()
        except Exception as e:  # noqa: BLE001 — the flusher must outlive any one failure
            logger.error("read-access log: flush failed (%s); queued reads kept for the next flush", e)


def flush_read_log() -> int:
    """Write every queued read; returns rows written. A batch that fails is
    logged LOUDLY (an audit gap is a §3 failure, not telemetry drift) and put back
    for the next flush."""
    if not _read_log_pending:
        return 0
    active_database, _dialect = _bind_read_log_deps()

    with _read_log_lock:
        batch = list(_read_log_pending)
        _read_log_pending.clear()
    if not batch:
        return 0
    by_store: dict[str, set[tuple[str, str, str | None, str]]] = {}
    for store, mid, agent, user, op in batch:
        by_store.setdefault(store, set()).add((mid, agent, user, op))  # a burst coalesces
    written = 0
    for store, rows in by_store.items():
        try:
            with active_database(store), _db() as db:
                d = _dialect()
                p = d.param()
                for mid, agent, user, op in rows:
                    db.execute(
                        f"INSERT INTO memory_access_log (id, memory_id, agent_id, user_id, op, ts) "
                        f"VALUES ({p}, {p}, {p}, {p}, {p}, {d.now()})",
                        (uuid.uuid4().hex, mid, agent, user, op),
                    )
                db.commit()
            written += len(rows)
        except Exception as e:  # noqa: BLE001 — requeue, report, keep the thread alive
            logger.error("read-access log: %d row(s) for %s not written (%s); retrying next flush", len(rows), store, e)
            with _read_log_lock:
                _read_log_pending.extend((store, *r) for r in rows)
    return written


import atexit as _atexit  # noqa: E402

_atexit.register(flush_read_log)  # the last reads before exit are not lost
