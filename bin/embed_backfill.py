#!/usr/bin/env python3
"""
embed_backfill.py — fill in missing embeddings for memory_items rows.

Companion to the M3_OBSERVER_NO_EMBED=1 ingest pattern. When ingest writes
rows without embedding (decoupling write throughput from embedder
throughput), this sweeper scans the DB for rows with no entry in
memory_embeddings and embeds them in batches.

Works on any m3-memory DB — the core memory store, a bench workspace,
a future fresh-ingestion DB, anywhere. Filter by --variant / --type /
--user-id / --scope / --id-prefix / --max-age-days to narrow scope.

Resumable by construction: the WHERE NOT EXISTS query IS the resume
marker. Crash mid-run, re-launch, picks up exactly where it left off.

Cost-free at the embedder side (uses local LLM_ENDPOINTS_CSV /
:8081 / LM Studio routing — no API charges).

Usage:

    # Sweep core memory (default DB) — embeds anything missing
    python bin/embed_backfill.py

    # Alternate workspace, only one variant
    python bin/embed_backfill.py \\
        --db memory/other.db \\
        --variant my-variant-name

    # Smoke test: 100 rows, dry-run
    python bin/embed_backfill.py --limit 100 --dry-run

    # Sharded sweepers (run multiple instances on disjoint id prefixes)
    python bin/embed_backfill.py --id-prefix 0 --lockfile /tmp/sweep0.lock &
    python bin/embed_backfill.py --id-prefix 1 --lockfile /tmp/sweep1.lock &

Hardening:
  - Per-batch timeout (--timeout-s)
  - Hard runtime cap (--max-runtime-min)
  - Auto-abort after N consecutive batch failures (--max-consecutive-fails)
  - Dim validation (--expected-dim) — won't write malformed embeddings
  - Per-row size cap (--max-row-bytes) — oversize content is subdivided and
    mean-pooled by default (--no-subdivide-oversize restores skipping)
  - Optional lockfile to prevent two sweepers racing on the same DB

This script is read-mostly + small bulk writes; safe to run alongside
an active enricher in WAL mode (SQLite handles concurrent reads fine).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import cast

from m3_core.paths import configured_main_db, deprecated_alias, pin_database, store_is_a_file  # noqa: E402
from m3_core.runtime import utc_iso

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
DEFAULT_DB = REPO_ROOT / "memory" / "agent_memory.db"
DEFAULT_BATCH_SIZE = 256
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_RUNTIME_MIN = 60
DEFAULT_MAX_CONSEC_FAILS = 5
DEFAULT_MAX_ROW_BYTES = 32_768  # bge-m3 ctx is 8192 tokens ≈ 32KB
DEFAULT_EXPECTED_DIM = 1024  # bge-m3 / qwen3-embed default
DEFAULT_CONN_REFRESH_BATCHES = 1000


# ── Status counters used in the final report ──────────────────────────────
# Re-exported from embed_sweep_lib so callers (including tests) keep using
# `embed_backfill.Counters`. The lib owns the canonical class.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from embed_sweep_lib import Counters as _LibCounters  # noqa: E402
from embed_sweep_lib import run_embed_loop


class _StandaloneSqliteDialect:
    """The few SQLite fragments this module needs, for a standalone run without
    the payload on sys.path. With the payload present every fragment comes from
    the active backend's dialect (see _sql)."""

    def param(self) -> str:
        return "?"

    def placeholder(self, n: int = 1) -> str:
        return ", ".join(["?"] * n)

    def byte_length(self, column: str) -> str:
        return f"LENGTH(CAST({column} AS BLOB))"

    def has_content(self, column: str) -> str:
        return f"LENGTH(TRIM(COALESCE({column}, ''))) > 0"

    def now_minus_days(self, days_placeholder: str) -> str:
        # strftime, not datetime(): the columns store "...T..Z" and a bare
        # datetime() bound matches every same-day row. Matches the seam.
        return f"strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-' || {days_placeholder} || ' days')"

    def table_exists(self, table: str) -> tuple:
        return ("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,))

    def columns_of(self, table: str) -> tuple:
        return (f"SELECT name FROM pragma_table_info('{table}')", ())


def _sql():
    """The active backend's dialect; the SQLite subset when standalone."""
    try:
        from memory.backends import dialect

        return dialect()
    except Exception:  # pragma: no cover - standalone fallback
        return _StandaloneSqliteDialect()


def _tables(args: argparse.Namespace) -> tuple[str, str]:
    """(items, embeddings) table names for the store being swept.

    On SQLite the chatlog is a separate file with the core table names, so
    `--db` alone picks the store. Other backends hold both stores in one
    database under different table names, so `--target` picks them.
    """
    if getattr(args, "target", "core") == "chatlog":
        try:
            from memory.backends import chatlog_table

            return chatlog_table("items"), chatlog_table("embeddings")
        except Exception:  # pragma: no cover - standalone fallback
            pass
    return "memory_items", "memory_embeddings"


def _is_core_store(args: argparse.Namespace) -> bool:
    """Whether the swept store is the core store, whose memories the core-only
    passes (deferred contradiction checks, unfindable and edited vectors)
    repair. Asked by capability: those passes read memory_history,
    which only the core store has. The table NAMES cannot tell: on SQLite the
    chatlog is a separate file with the core names, and `--db <chatlog>` ran
    the passes against it and crashed on the missing table."""
    items_table, _ = _tables(args)
    if items_table != "memory_items":
        return False
    with _bound_db(args.db) as conn:
        sql, params = _sql().table_exists("memory_history")
        return bool(conn.execute(sql, params).fetchone())


@contextmanager
def _bound_db(db_path):
    """Yield a seam connection bound to `db_path` for the duration of a block.

    This module sweeps an ARBITRARY `--db`, while `memory.db._db()` opens from
    the ACTIVE CONTEXT's pool — which is why every query here used to open its
    own raw `sqlite3.connect`. `active_database(path)` closes that gap: it binds
    the path via ContextVar for the block, so `_db()` targets the sweep's
    database and the module inherits the backend routing, the connection pool,
    and the pragma stack instead of hand-rolling all three.

    Falls back to a raw connection when the seam is unavailable (standalone
    execution without the payload on sys.path). Read-mostly queries only —
    writes still funnel through `mc._db()` in the write callback, as before.

    On a backend that is not file-per-database there is one pooled store and
    `db_path` does not select it; the backend's own connection is used.
    """
    try:
        from m3_core.context import pointed_store_connection
    except ImportError:  # pragma: no cover - standalone fallback
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()
        return
    # Never memory.db._db(): its first touch of a path migrates the database
    # this sweeper was merely pointed at (see pointed_store_connection).
    with pointed_store_connection(str(db_path) if store_is_a_file() else None) as conn:
        yield conn


def _db_for_display(value: str | None) -> str:
    """Render a DB location for a LOG line without leaking a password.

    `M3_DATABASE` is contractually a SQLite file path, but a misconfigured
    deployment can put a DSN there (`resolve_db_path` raises on exactly that),
    and a DSN carries credentials. Mirror the seam's own convention and echo a
    connection string scheme-only; a plain path is safe to show in full.
    """
    if value is None:
        return "<unset>"
    try:
        from m3_core.paths import _looks_like_dsn

        is_dsn = _looks_like_dsn(value)
    except Exception:  # pragma: no cover - standalone fallback
        is_dsn = "://" in value
    if is_dsn:
        return f"{value.strip().split('://', 1)[0]}://<redacted>"
    return repr(value)


@contextmanager
def _scoped_db_env(db_path):
    """`m3_sdk.scoped_db_env`, with a self-contained fallback.

    The seam owns the save/restore (see its docstring for why it is shared and
    not copied per caller). The fallback below is byte-identical in behaviour so
    a standalone run without the payload on sys.path still cannot leak.
    """
    try:
        from m3_sdk import scoped_db_env
    except Exception:  # pragma: no cover - standalone fallback
        prev = configured_main_db()
        if db_path is not None:
            pin_database(str(db_path))
        try:
            yield prev
        finally:
            if prev is None:
                pin_database(None)
            else:
                pin_database(prev)
        return
    with scoped_db_env(db_path) as prev:
        yield prev


@contextmanager
def _sweep_binding(db_path):
    """Bind the active database to `db_path` for a block, without a connection.

    `_bound_db` binds the path AND opens a connection, which suits the read
    queries but is the wrong shape for wrapping the whole `run_embed_loop`
    drive: that needs only the ContextVar so the write callback's `mc._db()`
    resolves to the swept store rather than to whatever `resolve_db_path(None)`
    would pick. Splitting the binding out keeps a long-lived pooled connection
    from being held open for the duration of the sweep.

    A no-op when the seam is unavailable (standalone execution without the
    payload on sys.path), mirroring `_bound_db`'s fallback: in that case the
    module is its own first importer, so the M3_DATABASE set by `_run_sweep`
    is still what every resolver sees.
    """
    try:
        from m3_sdk import active_database
    except Exception as e:  # pragma: no cover - standalone fallback
        # NOT a warning: this is the EXPECTED state for a standalone run with
        # no payload on sys.path, and a warning that fires on a healthy system
        # is itself a §3 violation. But it must not be silent either -- a
        # genuinely broken payload takes this same branch, and the consequence
        # is real: without the ContextVar the write callback's mc._db() routes
        # by M3_DATABASE, which _run_sweep restores on exit. So report it at
        # NOTE level, carrying the exception so the two cases are told apart by
        # whoever reads the log rather than by this branch guessing.
        _log(
            f"NOTE: seam unavailable ({type(e).__name__}: {e}); "
            f"observed: routing the sweep of {_db_for_display(str(db_path))} by "
            f"M3_DATABASE instead "
            f"of active_database(). expected for a standalone run without the "
            f"payload on sys.path. "
            f"inspect: if m3 IS installed, bin/ is missing from sys.path."
        )
        yield None
        return
    with active_database(str(db_path)) as resolved:
        yield resolved


class Counters(_LibCounters):
    """Backwards-compat shim: subclass of lib Counters with one extra attr.

    `cache_reuses` was tracked in the original embed_backfill but is
    informational only (never read by the loop). We keep it on the
    subclass so any downstream code that touched it doesn't break.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cache_reuses = 0  # rows whose content_hash already had a vector


# ── Lockfile ──────────────────────────────────────────────────────────────
@contextmanager
def _lockfile_guard(path: Path | None):
    """Refuse to start if another sweeper is running. No-op when path is None."""
    if path is None:
        yield
        return
    if os.path.exists(path):
        # Read pid + start time to give actionable error
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:
            content = "(unreadable)"
        raise RuntimeError(
            f"Lockfile already exists: {path}\n"
            f"Another sweeper may be running. Lockfile content: {content!r}\n"
            f"If you are sure no other sweeper is active, remove the file and retry."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()} {int(time.time())}", encoding="utf-8")
    try:
        yield
    finally:
        try:
            path.unlink()
        except Exception:
            pass


# ── Schema sanity check ───────────────────────────────────────────────────
def _verify_schema(db_path: Path, store: str = "core") -> None:
    """Confirm the target store has its items + embeddings tables in the shape
    we expect. Raise with a clear actionable message if not."""
    # No .exists() gate: on PostgreSQL there is no DB file, and raising
    # FileNotFoundError there would abort a backfill against a healthy store.
    d = _sql()
    items, embeddings = _tables(argparse.Namespace(store=store))
    required = {
        items: ("id", "content", "type", "variant", "user_id"),
        embeddings: ("memory_id", "embedding", "content_hash"),
    }
    with _bound_db(db_path) as conn:
        for tbl, cols in required.items():
            if conn.execute(*d.table_exists(tbl)).fetchone() is None:
                raise RuntimeError(
                    f"Table {tbl!r} not found in {db_path}. Run `python bin/migrate_memory.py --db {db_path} up` first. "
                    f"observed: no {tbl} table. inspect: the --db path, and its migrations"
                )
            have = {r[0] for r in conn.execute(*d.columns_of(tbl)).fetchall()}
            for col in cols:
                if col not in have:
                    raise RuntimeError(
                        f"{tbl}.{col} missing from {db_path}. DB schema is too old; run migrate_memory.py up."
                    )


# ── Build the candidate-row query ─────────────────────────────────────────
def _build_query(
    args: argparse.Namespace,
    after_id: str | None = None,
) -> tuple[str, list]:
    """Return (sql, params) for the candidate-rows SELECT.

    Selects rows that:
      - are not soft-deleted
      - have content (post-trim)
      - match optional filters (variant/type/user_id/scope/id-prefix/age)
      - have NO embedding row in memory_embeddings
      - have id > after_id when set (paginates past in-run skips)

    The after_id pagination matters: rows we skip mid-run (oversize,
    bad-dim, failed batch) still satisfy the NOT EXISTS predicate on
    the next cycle, so without forward progress on id we'd reselect
    them forever. Tracking the highest-id-seen-this-run and filtering
    `mi.id > ?` makes the sweep monotonic.

    NULL-safe on every column. Uses the existing index on
    memory_items.id (PK) and the memory_embeddings.memory_id index.
    """
    d = _sql()
    p = d.param()
    items, embeddings = _tables(args)
    where = [
        "COALESCE(mi.is_deleted, 0) = 0",
        d.has_content("mi.content"),
        f"NOT EXISTS (SELECT 1 FROM {embeddings} me WHERE me.memory_id = mi.id)",
    ]
    params: list = []

    # Exclude rows the sweep would only skip as oversize. Without this the row
    # stays in the candidate set (and in _count_pending) forever: after_id keeps
    # ONE run monotonic, but the next run reselects it, so has_embed_work() in
    # m3_cognitive_loop reports work that can never drain. That kept the loop on
    # its 1s idle-backlog floor indefinitely (2026-08-09: 3 rows @ 39-44KB,
    # 174MB of logs). Mirrors the per-row cap applied during the sweep, so the
    # gate and the sweep now agree on what "pending" means.
    #
    # The backend-agnostic protection against the same livelock is
    # _MAX_DRAIN_TICKS in m3_cognitive_loop, which bounds the idle-drain burst
    # no matter which store reports the un-drainable work.
    # Only exclude them when the sweep would genuinely drop them. With
    # --subdivide-oversize (the default) they ARE drainable — embed_many splits
    # and mean-pools them — so they must stay in the candidate set and in
    # _count_pending, or the loop would never wake up to process them.
    max_row_bytes = getattr(args, "max_row_bytes", None)
    if max_row_bytes and not getattr(args, "subdivide_oversize", False):
        where.append(f"{d.byte_length('mi.content')} <= {p}")
        params.append(int(max_row_bytes))

    if after_id is not None:
        where.append(f"mi.id > {p}")
        params.append(after_id)
    if args.variant:
        where.append(f"mi.variant IN ({d.placeholder(len(args.variant))})")
        params.extend(args.variant)
    if args.type:
        where.append(f"mi.type IN ({d.placeholder(len(args.type))})")
        params.extend(args.type)
    if args.user_id:
        where.append(f"COALESCE(mi.user_id, '') = {p}")
        params.append(args.user_id)
    if args.scope:
        where.append(f"COALESCE(mi.scope, '') = {p}")
        params.append(args.scope)
    if args.id_prefix:
        where.append(f"mi.id LIKE {p}")
        params.append(f"{args.id_prefix.lower()}%")
    if args.max_age_days is not None:
        # Older than N days = created_at < (now - N days). The expression comes
        # from the seam: `datetime('now', ?)` binding a "-N days" MODIFIER
        # STRING is SQLite-only and raises on PostgreSQL. now_minus_days() binds
        # a plain INTEGER instead, which both dialects accept.
        where.append(f"mi.created_at < {d.now_minus_days(p)}")
        params.append(int(args.max_age_days))

    sql = f"""
        SELECT mi.id, mi.content, mi.title, mi.metadata_json
        FROM {items} mi
        WHERE {" AND ".join(where)}
        ORDER BY mi.id
        LIMIT {p}
    """
    return sql, params


# ── Pre-flight count ──────────────────────────────────────────────────────
def _count_pending(db_path: Path, args: argparse.Namespace) -> int:
    """How many rows would be in scope?  Cheap; uses same WHERE clause."""
    sql, params = _build_query(args)
    # Replace the LIMIT with a COUNT.
    count_sql = sql.replace("SELECT mi.id, mi.content, mi.title, mi.metadata_json", "SELECT COUNT(*)")
    # Strip ORDER BY + LIMIT (we don't pass a LIMIT param for count)
    count_sql = count_sql.split("ORDER BY")[0]
    with _bound_db(db_path) as conn:
        return cast("int", conn.execute(count_sql, params).fetchone()[0])


def count_oversize_excluded(db_path: Path, args: argparse.Namespace) -> int:
    """Rows with no embedding that the size cap excludes from _count_pending.

    Only meaningful in --no-subdivide-oversize mode, where such a row is a real,
    permanent gap in embedding coverage: not a *drainable* backlog, which is why
    the work-gate must not count it, but visible here so "pending: 0" never
    silently means "rows we gave up on". With the default --subdivide-oversize
    these rows are drainable (embed_many subdivides and mean-pools them), so
    they count as ordinary pending work and this returns 0.
    """
    max_row_bytes = getattr(args, "max_row_bytes", None)
    if not max_row_bytes or getattr(args, "subdivide_oversize", False):
        return 0
    d = _sql()
    items, embeddings = _tables(args)
    sql = f"""
        SELECT COUNT(*) FROM {items} mi
        WHERE COALESCE(mi.is_deleted, 0) = 0
          AND {d.has_content("mi.content")}
          AND NOT EXISTS (
              SELECT 1 FROM {embeddings} me WHERE me.memory_id = mi.id)
          AND {d.byte_length("mi.content")} > {d.param()}
    """
    try:
        with _bound_db(db_path) as conn:
            return cast("int", conn.execute(sql, (int(max_row_bytes),)).fetchone()[0])
    except Exception:  # noqa: BLE001 — informational count; never fail the sweep
        return 0


# ── Main async loop ───────────────────────────────────────────────────────
async def _run_sweep(args: argparse.Namespace, counters: Counters) -> int:
    """Delegate to embed_sweep_lib.run_embed_loop with the embed_backfill-
    specific fetch + write callbacks.

    Late import: memory_core (via memory.config.DB_PATH) reads M3_DATABASE at
    import time, so the env var must be set BEFORE the import below — that is
    what the assignment here is for, and only a FIRST importer benefits from it
    (standalone `embed_backfill` runs). It is NOT the routing mechanism: the
    sweep is bound to `args.db` by `active_database()` in the block below, which
    outranks the env var in `resolve_db_path()` and unwinds cleanly.

    ⚠ THE ENV WRITE IS RESTORED ON EXIT, AND MUST STAY THAT WAY (#180).
    Leaking it poisons every LATER `resolve_db_path(None)` in the process. In
    the cognitive loop that is not hypothetical: the loop sweeps each store in
    turn, so the last value left behind was the CHATLOG db, and
    `chatlog_config.chat_store_paths()` — whose "main store" entry comes from
    `resolve_db_path(None)` — then returned the chatlog path as main and
    collapsed to a single entry. `_embed_target_dbs(args.database)` substitutes
    its own main over that entry, so the chatlog store dropped out of the list
    entirely, `has_embed_work()` read the (clean) main db, reported False, and
    the idle-backlog-drain never fired. Unembedded chat turns then accumulated
    until a restart cleared the env. Note the loop imports memory_core long
    before this pass runs, so there the assignment binds nothing and leaked
    only the damage.
    """
    with _scoped_db_env(args.db) as _prev_db_env:
        try:
            return await _run_sweep_inner(args, counters)
        finally:
            # Report a value we did NOT set: we own M3_DATABASE for the
            # duration of this call, and the restore is about to HIDE any
            # change to it. §3 evidence levels -- we OBSERVED the value, we
            # did NOT confirm who wrote it, so the writer is `possible:`, not
            # `cause:`. Quiet on a healthy sweep (a false alarm is itself a §3
            # violation).
            _observed = configured_main_db()
            if _observed != str(args.db):
                _log(
                    f"WARNING: M3_DATABASE changed during the sweep. "
                    f"observed: {_db_for_display(_observed)}, "
                    f"expected {_db_for_display(str(args.db))}; "
                    f"restoring to {_db_for_display(_prev_db_env)}. "
                    f"possible: a callee set the env var directly instead of "
                    f"using active_database(); such a write outlives this call "
                    f"and steers later resolve_db_path(None) at the wrong "
                    f"store (#180). "
                    f"inspect: os.environ['M3_DATABASE'] writers on the "
                    f"embed path."
                )


async def _run_sweep_inner(args: argparse.Namespace, counters: Counters) -> int:
    """Body of :func:`_run_sweep`, with M3_DATABASE set for the import below."""
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))

    import memory_core as mc  # noqa: E402

    started = time.monotonic()
    deadline = started + args.max_runtime_min * 60.0

    # ── Fetch callback ────────────────────────────────────────────────
    # Pulls candidates from args.db using _build_query. The helper passes
    # us the high-water mark (after_id) and a fetch size; we just plug
    # them into the existing SQL builder.
    check_core = _is_core_store(args)

    def _fetch(after_id, limit):
        sql, params = _build_query(args, after_id=after_id)
        params_with_limit = [*params, limit]
        with _bound_db(args.db) as conn:
            return conn.execute(sql, params_with_limit).fetchall()

    # ── Write callback ────────────────────────────────────────────────
    # Persists one embedding row. Uses mc._db() so writes funnel through
    # memory_core's connection pool (keeps WAL / busy_timeout behavior
    # consistent with the rest of the codebase). Returns True iff a row
    # was newly written.
    # Through the backend's one embedding write path, so a backend that keeps
    # more than the blob (PostgreSQL's pgvector copy) gets it here too.
    from memory.backends import active_backend
    from memory.backends.base import EmbeddingRow

    _, embeddings = _tables(args)

    def _write(mid: str, vec: list[float], model_str: str, content_hash: str) -> bool:
        row = EmbeddingRow(_new_uuid(), mid, mc._pack(vec), model_str, len(vec), utc_iso(), content_hash)
        with mc._db() as db:
            # The id is fresh, so the only conflict key cannot collide: the row
            # is always written.
            return bool(active_backend().insert_embeddings(db, [row], on_conflict="ignore", table=embeddings))

    # ── Text transform callback ───────────────────────────────────────
    # Match memory_write_impl's inline behavior: augment with anchors.
    # Skip when --no-augment-anchors set.
    if args.no_augment_anchors:
        transform = None  # use lib default (identity)
    else:

        def transform(text: str, metadata) -> str:
            return cast("str", mc._augment_embed_text_with_anchors(text, metadata))

    # Drive the loop.
    #
    # Bound to args.db for the whole drive, not just the read queries: the
    # `_write` callback below uses `mc._db()`, which resolves through the active
    # context. Before #180 that resolution happened to land on the right store
    # only because `_run_sweep` had leaked M3_DATABASE process-wide; now that the
    # leak is restored on exit, the binding has to be explicit here or writes
    # would target the MAIN db while reads came from the swept one. ContextVars
    # propagate across `await` within a task, so this covers the whole drive.
    # Not needed on a backend with one pooled store: mc._db() reaches it anyway.
    with _sweep_binding(args.db) if store_is_a_file() else nullcontext():
        await run_embed_loop(
            fetch_candidates=_fetch,
            write_embedding=_write,
            counters=counters,
            embed_many=mc._embed_many,
            content_hash_fn=mc._content_hash,
            transform_text=transform if transform is not None else (lambda t, _m: t),
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            timeout_s=args.timeout_s,
            deadline_s=deadline,
            max_consecutive_fails=args.max_consecutive_fails,
            max_row_bytes=args.max_row_bytes,
            oversize_mode=("subdivide" if getattr(args, "subdivide_oversize", False) else "skip"),
            expected_dim=(args.expected_dim or None),
            limit=args.limit,
            log=_log,
        )
        if check_core and not args.dry_run:
            # Owed checks first: each is a promise a write recorded, while the
            # repairs are opportunistic. Every step runs even when an earlier one
            # fails; the failures come back together as PostSweepError, so the
            # caller can say the embeddings themselves landed.
            steps = (
                ("owed contradiction checks", _run_owed_contradiction_checks),
                ("unfindable-memory repair", _repair_unfindable),
                ("stale-after-edit repair", _repair_stale_after_edit),
            )
            failed: list[tuple[str, BaseException]] = []
            for label, step in steps:
                try:
                    await step(args)
                except Exception as e:  # noqa: BLE001 - collected and raised below
                    failed.append((label, e))
            if failed:
                raise PostSweepError(_db_for_display(str(args.db)), [label for label, _ in steps], failed)
    return 0


class PostSweepError(RuntimeError):
    """The sweep wrote its embeddings; one or more core-store steps after it failed.
    Keeps each step's exception so the caller can log its traceback."""

    def __init__(self, store: str, steps: list[str], failed: list[tuple[str, BaseException]]):
        self.store = store
        self.failed = failed
        bad = {label for label, _ in failed}
        ok = [s for s in steps if s not in bad]
        # Without the store, for a caller whose own message already names it.
        self.summary = (
            "; ".join(f"{label} ({type(e).__name__}: {e})" for label, e in failed)
            + f". completed: {', '.join(ok) if ok else 'none'}"
        )
        super().__init__(f"failed on {store}: {self.summary}")


_OWED_CHECKS_PER_SWEEP = 1000


async def _run_owed_contradiction_checks(args: argparse.Namespace) -> None:
    """Run the contradiction checks writes deferred for want of a vector, for every
    such row that now has one, read from the store (memory.write.owed_contradiction_checks).
    A row whose check raises does not stop the others; the failures are raised
    together after the rest have run, and the flag stays set so the next sweep
    tries them again."""
    from memory.write import owed_contradiction_checks, run_deferred_contradiction_check

    store = _db_for_display(str(args.db))
    with _bound_db(args.db) as conn:
        due = owed_contradiction_checks(conn, limit=_OWED_CHECKS_PER_SWEEP)
    if not due:
        return
    checked = superseded = 0
    not_run: list[str] = []
    errors: list[tuple[str, Exception]] = []
    for mid in due:
        try:
            got = await run_deferred_contradiction_check(mid)
        except Exception as e:  # noqa: BLE001 - one row must not stall the rest; raised below
            errors.append((mid, e))
            continue
        if got is None:
            not_run.append(mid)
        else:
            checked += 1
            superseded += len(got)
    more = (
        f"; capped at {_OWED_CHECKS_PER_SWEEP}, the rest run next sweep" if len(due) == _OWED_CHECKS_PER_SWEEP else ""
    )
    _log(
        f"deferred contradiction checks on {store}: {len(due)} owed, {checked} checked, "
        f"{superseded} memories superseded, {len(not_run)} not run, {len(errors)} failed{more}"
        + (
            f". observed: not run (the row lost its vector or its flag between the read and the check): {not_run[:5]}"
            if not_run
            else ""
        )
    )
    if errors:
        first_id, first = errors[0]
        raise RuntimeError(
            f"{len(errors)} of {len(due)} owed checks raised; first: {first_id} "
            f"({type(first).__name__}: {first}); ids: {[i for i, _ in errors[:5]]}. "
            f"Their flags stay set and the next sweep retries them. inspect: memory_get {first_id}"
        ) from first


async def _repair_unfindable(args: argparse.Namespace) -> None:
    """Re-window live memories that have embeddings but no 'default' vector.

    Memories windowed before 2026.9.8.0 carry window_N rows and no 'default', so
    default search cannot reach them, and the sweep above skips them (they HAVE
    embeddings). The predicate is the doctor's own (unfindable_memories, one
    owner); the repair is the write path's own (rewindow_memory).
    """
    from doctor.embed_space_probe import unfindable_memories
    from memory.write import rewindow_memory

    d = _sql()
    with _bound_db(args.db) as conn:
        cols = {r[0] for r in conn.execute(*d.columns_of("memory_embeddings")).fetchall()}
        if "vector_kind" not in cols:
            # A store older than migration 022 has no vector kinds, so it cannot
            # hold the pre-9.8 windowed rows this repairs. Say so; never fail the
            # backfill that already ran over it.
            _log("unfindable-memory repair skipped: memory_embeddings has no vector_kind column")
            return
        ids = [r[0] for r in unfindable_memories(conn, d, limit=100_000)]
    if not ids:
        return
    repaired = 0
    for mid in ids:
        if await rewindow_memory(mid):
            repaired += 1
    _log(f"unfindable memories (embeddings but no 'default' vector): {len(ids)} found, {repaired} re-windowed")


async def _repair_stale_after_edit(args: argparse.Namespace) -> None:
    """Re-embed live memories whose text was edited after their newest vector.

    The predicate is the doctor's own (stale_vector_memories, one owner); the
    repair is the write path's own (rewindow_memory). A memory the embedder
    returns nothing for keeps its rows and is tried again on the next pass.
    """
    from doctor.embed_space_probe import stale_vector_memories
    from memory.write import rewindow_memory

    with _bound_db(args.db) as conn:
        ids = stale_vector_memories(conn, _sql(), limit=100_000)
    if not ids:
        return
    refreshed = 0
    for mid in ids:
        if await rewindow_memory(mid):
            refreshed += 1
    _log(f"memories edited after their vector: {len(ids)} found, {refreshed} re-embedded")


# ── Helpers ───────────────────────────────────────────────────────────────
def _log(msg: str, ts: str | None = None) -> None:
    """ASCII-safe stdout. Avoids the cp1252 trap on Windows."""
    if ts is None:
        ts = utc_iso()
    line = f"[{ts}] [embed_backfill] {msg}\n"
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.write(line.encode("ascii", "replace").decode("ascii"))
        sys.stdout.flush()


def _new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def _print_report(counters: Counters, started_at: float, db_path: Path, args: argparse.Namespace | None = None) -> None:
    elapsed = time.monotonic() - started_at
    rate = counters.embedded / max(elapsed, 1e-3)
    print()
    print("=" * 64)
    print("  embed_backfill COMPLETE")
    print("=" * 64)
    print(f"  db:                {db_path}")
    print(f"  scanned:           {counters.scanned}")
    print(f"  embedded:          {counters.embedded}")
    print(f"  skipped (empty):   {counters.skipped_empty}")
    print(f"  skipped (oversize):{counters.skipped_oversize}")
    if getattr(counters, "oversize_subdivided", 0):
        print(f"  subdivided (big):  {counters.oversize_subdivided}  <- embedded via split + mean-pool")
    print(f"  skipped (bad dim): {counters.skipped_bad_dim}")
    _excluded = count_oversize_excluded(db_path, args) if args is not None else 0
    if _excluded:
        print(
            f"  EXCLUDED oversize: {_excluded}  <- never embedded; over "
            f"--max-row-bytes={getattr(args, 'max_row_bytes', 0)}. Not counted "
            f"as pending (they cannot drain); needs the subdivide path."
        )
    print(f"  failed batches:    {counters.failed_batches}")
    print("  cache reuses:      (handled by _embed_many internal cache)")
    print(f"  wall time:         {elapsed:.1f}s")
    print(f"  effective rate:    {rate:.1f} embeds/s")
    if counters.errors_by_class:
        print()
        print("  Error breakdown:")
        for cls, n in sorted(counters.errors_by_class.items(), key=lambda kv: kv[1], reverse=True):
            print(f"    {cls:<32} {n}")
    print()


# ── argparse + main ───────────────────────────────────────────────────────
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Selection
    sel = ap.add_argument_group("selection")
    sel.add_argument(
        "--db",
        type=Path,
        default=Path(configured_main_db() or str(DEFAULT_DB)),
        help=f"Target DB. Default: $M3_DATABASE or {DEFAULT_DB}",
    )
    sel.add_argument(
        "--target",
        choices=("core", "chatlog"),
        default="core",
        help="Which tables to sweep on a backend that keeps both in one "
        "database (PostgreSQL). On SQLite --db selects the file.",
    )
    sel.add_argument(
        "--store",
        dest="target",
        choices=("core", "chatlog"),
        action=deprecated_alias("--target"),
        help=argparse.SUPPRESS,
    )
    sel.add_argument("--variant", action="append", default=[], help="Filter to one variant. Repeatable for OR.")
    sel.add_argument("--type", action="append", default=[], help="Filter to one memory type. Repeatable for OR.")
    sel.add_argument("--user-id", type=str, default=None, help="Filter to one user_id.")
    sel.add_argument("--scope", type=str, default=None, help="Filter to one scope (user/session/agent/org).")
    sel.add_argument(
        "--id-prefix",
        type=str,
        default=None,
        help="Backfill only rows whose id starts with this hex prefix. Use to shard across multiple sweeper instances.",
    )
    sel.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="Only rows older than N days. Useful when you want to leave fresh writes alone for a window first.",
    )
    sel.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after AT LEAST N successful embeds. The check "
        "fires at outer-cycle boundaries, so the actual stop "
        "point can overshoot by up to one cycle's fetch "
        "(batch_size * concurrency * 4 = "
        f"{DEFAULT_BATCH_SIZE * DEFAULT_CONCURRENCY * 4} rows "
        "at defaults). Used for smoke testing — for strict "
        "row caps, also lower --batch-size and --concurrency.",
    )

    # Performance
    perf = ap.add_argument_group("performance")
    perf.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per embed call. Default: {DEFAULT_BATCH_SIZE}.",
    )
    perf.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Concurrent batches in flight. Default: {DEFAULT_CONCURRENCY}. "
        f"Cap by your llama-server's --parallel slots.",
    )
    perf.add_argument(
        "--connection-refresh",
        type=int,
        default=DEFAULT_CONN_REFRESH_BATCHES,
        help=f"Batches between connection-pool recycle. Default: {DEFAULT_CONN_REFRESH_BATCHES}.",
    )

    # Resilience
    res = ap.add_argument_group("resilience")
    res.add_argument(
        "--timeout-s",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Per-batch embed call timeout. Default: {DEFAULT_TIMEOUT_S}s.",
    )
    res.add_argument(
        "--max-runtime-min",
        type=int,
        default=DEFAULT_MAX_RUNTIME_MIN,
        help=f"Hard kill at N min wall-clock. Default: {DEFAULT_MAX_RUNTIME_MIN}.",
    )
    res.add_argument(
        "--max-consecutive-fails",
        type=int,
        default=DEFAULT_MAX_CONSEC_FAILS,
        help=f"Abort after N back-to-back batch fails. Default: {DEFAULT_MAX_CONSEC_FAILS}.",
    )
    res.add_argument(
        "--max-row-bytes",
        type=int,
        default=DEFAULT_MAX_ROW_BYTES,
        help=f"Size threshold for a row's post-transform text. "
        f"Default: {DEFAULT_MAX_ROW_BYTES} (bge-m3 ctx limit). "
        f"See --subdivide-oversize for what happens above it.",
    )
    res.add_argument(
        "--subdivide-oversize",
        dest="subdivide_oversize",
        action="store_true",
        default=True,
        help="Embed rows over --max-row-bytes by splitting them "
        "and mean-pooling the sub-vectors, instead of "
        "skipping them. Default: on.",
    )
    res.add_argument(
        "--no-subdivide-oversize",
        dest="subdivide_oversize",
        action="store_false",
        help="Drop rows over --max-row-bytes (pre-2026.8.19.5 "
        "behaviour). They are then excluded from the pending "
        "count so they cannot livelock the cognitive loop, "
        "and reported separately as EXCLUDED oversize.",
    )
    res.add_argument(
        "--expected-dim",
        type=int,
        default=DEFAULT_EXPECTED_DIM,
        help=f"Skip embeddings whose dim != N. Default: {DEFAULT_EXPECTED_DIM}. Pass 0 to disable.",
    )
    res.add_argument(
        "--lockfile",
        type=Path,
        default=None,
        help="Refuse to start if this file exists; create it on start, "
        "delete on clean exit. Use for cron / scheduled sweepers.",
    )

    # Behavior
    beh = ap.add_argument_group("behavior")
    beh.add_argument(
        "--no-augment-anchors",
        action="store_true",
        help="Skip _augment_embed_text_with_anchors before embed. "
        "Default OFF — anchors match memory_write_impl behavior.",
    )
    beh.add_argument("--dry-run", action="store_true", help="Print plan and counts; don't embed or write.")

    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Schema sanity
    try:
        _verify_schema(args.db, args.target)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # Plan
    pending = _count_pending(args.db, args)
    print(f"DB:       {args.db}" if store_is_a_file() else f"Target:   {args.target} (one database)")
    print(f"Pending:  {pending}")
    print("Filters:")
    if args.variant:
        print(f"  variant: {args.variant}")
    if args.type:
        print(f"  type: {args.type}")
    if args.user_id:
        print(f"  user_id: {args.user_id}")
    if args.scope:
        print(f"  scope: {args.scope}")
    if args.id_prefix:
        print(f"  id_prefix: {args.id_prefix!r}")
    if args.max_age_days is not None:
        print(f"  max_age_days: {args.max_age_days}")
    if args.limit:
        print(f"  limit: {args.limit}")
    print(f"Performance: batch_size={args.batch_size} concurrency={args.concurrency}")
    print(
        f"Resilience:  timeout={args.timeout_s}s max_runtime={args.max_runtime_min}min "
        f"max_consec_fails={args.max_consecutive_fails}"
    )
    if args.dry_run:
        print()
        print("(dry-run: no embeddings written)")
        return 0

    if pending == 0:
        print()
        print("No rows pending; nothing to do.")
        return 0

    # Lockfile guard + run
    counters = Counters()
    started_at = time.monotonic()
    try:
        with _lockfile_guard(args.lockfile):
            asyncio.run(_run_sweep(args, counters))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        _log("INTERRUPTED: caught Ctrl-C; reporting partial run.")

    _print_report(counters, started_at, args.db, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
