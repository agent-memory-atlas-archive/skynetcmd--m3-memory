"""PostgreSQL `StorageBackend` — pooled, baseline (extension-free) implementation.

Phase 1, baseline-only. This backend exists so the same memory core can serve
10-100s of concurrent readers/writers on one live store (the SQLite single-writer
ceiling is the reason PostgreSQL is offered at all — plan §0). It is opt-in;
SQLite remains the default (DESIGN_PHILOSOPHIES §1).

Design contract — identical to the SQLite path so callers don't branch:
  * ``connection()`` is a ``@contextmanager`` yielding a DB-API connection, with
    the SAME commit-on-success / rollback-on-exception / return-to-pool
    discipline as ``memory.db._db()`` (see ``M3Context.get_sqlite_conn``).
  * A real CONNECTION POOL (``psycopg2.pool.ThreadedConnectionPool``) — the
    Phase 1 blocker: single-connect-per-request cannot serve the concurrency
    this backend is for.

Baseline means NO server extensions required (plan §5): vectors are BYTEA +
Rust cosine, keyword is tsvector/GIN — both work on a vanilla, locked-down
federal PostgreSQL. pgvector / pg_search are Phase 4 accelerators behind probes.

Cycle-break (§2): no top-level import of ``memory_core``; resolve lazily.
Fail loud (§3): a missing DSN or driver raises with an actionable message, never
a silent fallback to SQLite.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from m3_core.config import settings as _m3cfg

from .base import (
    BackendName,
    Capabilities,
    KeywordHit,
    Snapshot,
    SnapshotError,
    VectorHit,
    snapshot_target,
)
from .dialect import Dialect, ParamStyle
from .registry import register_backend

if TYPE_CHECKING:  # avoid importing the driver at module load
    from psycopg2.pool import ThreadedConnectionPool


# Pool sizing. minconn keeps a few warm; maxconn caps concurrency so a runaway
# fan-out can't exhaust the server's max_connections. Overridable by env for the
# federal 10-100s-user tier. These are conservative dev defaults.
_DEFAULT_MINCONN = 1
_DEFAULT_MAXCONN = 16

# Rows per multi-row INSERT in bulk_upsert. execute_values loops across pages
# internally, so this bounds the size of ONE statement rather than the batch:
# large enough that per-round-trip latency stops dominating, small enough that a
# single statement stays comfortably parseable and a failure rolls back a
# bounded amount of work. Overridable for tuning against a slow link.
_UPSERT_PAGE_SIZE = _m3cfg.setting("db.postgres.upsert_page_size")


def _make_compat_cursor_factory():
    """A psycopg2 cursor whose rows behave like ``sqlite3.Row``: subscriptable by
    BOTH position (``row[0]``) AND column name (``row["col"]``), and iterable /
    len-able as a tuple.

    The memory core (written against ``sqlite3.Row``) mixes both access styles —
    the write path uses ``row["content"]`` while trust/graph/etc. use ``row[0]``.
    psycopg2's default cursor gives tuples (no name access); ``RealDictCursor``
    gives dicts (no positional access). Neither alone matches ``sqlite3.Row``, so
    a row that fails one style is a latent bug on PG (e.g. ``row[0]`` -> KeyError
    under RealDictCursor). This factory returns rows supporting both.
    """
    import psycopg2.extensions

    class _DualRow(tuple):
        """A result row: a tuple (positional) that also maps column name -> value.

        No ``__slots__`` — a tuple subclass can't have non-empty slots, so
        ``_columns`` lives in the instance ``__dict__``.
        """

        def __new__(cls, values, columns):
            self = super().__new__(cls, values)
            self._columns = columns
            return self

        @override
        def __getitem__(self, key):
            if isinstance(key, str):
                return tuple.__getitem__(self, self._columns[key])
            return tuple.__getitem__(self, key)

        def get(self, key, default=None):
            try:
                return self[key]
            except (KeyError, IndexError):
                return default

        def keys(self):
            return list(self._columns.keys())

    class _DualCursor(psycopg2.extensions.cursor):
        """Cursor emitting _DualRow instances (sqlite3.Row-like access)."""

        def _colmap(self):
            if self.description is None:
                return {}
            return {d[0]: i for i, d in enumerate(self.description)}

        def fetchone(self):
            row = super().fetchone()
            return None if row is None else _DualRow(row, self._colmap())

        def fetchall(self):
            cols = self._colmap()
            return [_DualRow(r, cols) for r in super().fetchall()]

        def fetchmany(self, size=None):
            cols = self._colmap()
            rows = super().fetchmany(size) if size is not None else super().fetchmany()
            return [_DualRow(r, cols) for r in rows]

        def __iter__(self):
            # Drive the BASE fetchmany, never super().__iter__().
            #
            # psycopg2's C cursor implements __iter__ by returning SELF, so
            # `for r in super().__iter__()` iterates this _DualCursor again and
            # re-enters this very method: unbounded recursion, surfacing as
            # `RecursionError: maximum recursion depth exceeded` from whatever
            # innocent line did `for r in conn.execute(...)`. Measured 2026-09-29;
            # 31 call sites in bin/ iterate a cursor that way, so every one of
            # them was broken on a PostgreSQL primary while `.fetchall()` worked
            # -- which is why it stayed hidden.
            #
            # super().fetchmany() is the driver's own, NOT this class's override,
            # so the rows come back as plain tuples and get wrapped exactly once.
            # Batched rather than fetchall() so a large result still streams.
            cols = self._colmap()
            while True:
                rows = super().fetchmany()
                if not rows:
                    return
                for r in rows:
                    yield _DualRow(r, cols)

    return _DualCursor


# NOTE: this shim deliberately does NOT translate '?'->'%s'. sqlite-style qmark
# SQL must be ported to dialect().param() at the call site (the critical-path
# _impl directive) — a runtime translator was tried and removed 2026-07-24: it
# added a per-execute string scan on the hot path plus a hand-rolled SQL-literal
# parser (a silent correctness surface), and it MASKED the loud PG SyntaxError
# that is the useful signal "this query still needs porting to the seam". A raw
# '?' reaching PG is meant to fail loudly, not be silently rewritten.
class _SqliteCompatConnection:
    """Wrap a psycopg2 connection to present the SQLite-connection surface the
    memory core expects, so ``db.execute(...)`` and ``row["col"]``/``row[0]`` both
    work unchanged.

    The core was written against ``sqlite3.Connection`` with ``row_factory=Row``:
      * ``conn.execute(sql, params)`` is a shortcut that creates a cursor, runs
        the statement, and returns the CURSOR (so ``.fetchone()``/``.fetchall()``/
        ``.rowcount`` work on the result). psycopg2 has no connection-level
        ``execute`` — only cursors do.
      * rows are accessed by NAME (``row["content"]``) AND by POSITION (``row[0]``)
        across the codebase, exactly as ``sqlite3.Row`` allows. A ``_DualCursor``
        (see ``_make_compat_cursor_factory``) emits rows supporting both.
    This adapter closes those gaps. It is intentionally minimal — only the surface
    the memory paths actually use — not a general DB-API façade.
    """

    def __init__(self, raw_conn: object) -> None:
        self._raw = raw_conn

    def execute(self, sql: str, params: tuple | list = ()):  # noqa: ANN001
        cur = self._raw.cursor(  # type: ignore[attr-defined]
            cursor_factory=_make_compat_cursor_factory()
        )
        # SQL is passed verbatim: callers must use dialect().param() ('%s' on PG).
        # A raw '?' here raises a loud psycopg2 SyntaxError by design — that is the
        # signal to port the query to the seam, not to silently rewrite it.
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq_of_params):  # noqa: ANN001
        """sqlite3.Connection.executemany shortcut, which psycopg2 connections
        lack (executemany is a cursor method there). The memory/chatlog write
        paths call conn.executemany(sql, rows); mirror execute() so they work
        unchanged on PG. psycopg2's cursor.executemany runs the statement per row.
        """
        cur = self._raw.cursor(  # type: ignore[attr-defined]
            cursor_factory=_make_compat_cursor_factory()
        )
        cur.executemany(sql, seq_of_params)  # verbatim; use dialect().param()
        return cur

    def cursor(self, *args, **kwargs):
        return self._raw.cursor(*args, **kwargs)

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()

    def __getattr__(self, name):
        # Anything not explicitly adapted falls through to the raw connection.
        return getattr(self._raw, name)


def _forbidden_pg_hosts() -> list[str]:
    """Hosts the PRIMARY store must never connect to (defense in depth).

    The data-warehouse / CDW hub is a shared fan-in mirror; a primary-store DSN
    pointing at it would read/write production warehouse data. Configurable via
    ``M3_PG_FORBIDDEN_HOSTS`` (comma-separated). Empty by default here — the
    deployment supplies its known warehouse host(s); tests supply the dev hub.
    """
    return [h.strip() for h in _m3cfg.setting("db.postgres.forbidden_hosts") if h.strip()]


def _reject_forbidden_host(url: str) -> None:
    """Raise if ``url`` targets a forbidden host (see ``_forbidden_pg_hosts``).

    Matches on the PARSED host (exact, case-insensitive), so a forbidden
    ``198.51.100.5`` does not spuriously reject ``198.51.100.51``, and a forbidden
    host string appearing only inside a password does not trigger. Falls back to
    a raw substring check only when the DSN can't be parsed — better to over-
    refuse an unparseable DSN than to let a forbidden host slip through."""
    forbidden = _forbidden_pg_hosts()
    if not forbidden:
        return
    from m3_core.dsn import parse_pg_dsn

    parts = parse_pg_dsn(url)
    # libpq takes a comma-separated host list and tries each: any of them counts.
    parsed_hosts = {h.strip().lower() for h in parts.host.split(",")} - {""} if parts else set()
    scheme = "postgres" if "://" not in url else url.strip().split("://", 1)[0]

    for host in forbidden:
        if not host:
            continue
        hit = (host.lower() in parsed_hosts) if parsed_hosts else (host in url)
        if hit:
            # Scheme and host only — NEVER echo the full DSN (contains password)
            raise RuntimeError(
                f"observed: PostgreSQL PRIMARY-store DSN targets forbidden "
                f"host {host!r} (from M3_PG_FORBIDDEN_HOSTS). cause: the DSN "
                f"is configured to "
                f"connect to the data-warehouse/CDW mirror instead of the primary "
                f"store. inspect: M3_PRIMARY_PG_URL (current: {scheme}://..., "
                f"host={host}) should point to a dedicated primary database, not "
                f"a warehouse mirror. Point M3_PRIMARY_PG_URL at a different host."
            )


def _resolve_dsn() -> str:
    """Resolve the PRIMARY-store PostgreSQL DSN, fail loud if absent.

    Role-separated (see m3_core.paths): ``M3_PRIMARY_PG_URL`` > ``M3_PG_URL`` >
    encrypted vault. It NEVER reads ``PG_URL`` or any CDW/warehouse var — the
    warehouse DSN must not reach the primary store. The resolved DSN is then
    checked against ``M3_PG_FORBIDDEN_HOSTS`` (the known warehouse host(s)). No
    default — selecting the postgres backend without a DSN is a configuration
    error, not a reason to silently do something else.
    """
    # The one DSN owner: env > keyring > secrets file > vault (deprecated for the
    # primary), under the PRIMARY role's names only, so a stored warehouse DSN
    # (PG_URL) can never become the primary store.
    from m3_core.secret_store import PgRole, explain_pg_dsn

    found = explain_pg_dsn(PgRole.PRIMARY)
    url = found.dsn
    if not url:
        # What each tier hit, not just "not set": a broken keyring or vault used
        # to be swallowed here and reported as a missing DSN (m3 2f4bad6c).
        hit = ("; tiers reported: " + " | ".join(found.errors)) if found.errors else ""
        raise RuntimeError(
            "observed: PostgreSQL backend selected (M3_DB_BACKEND=postgres) but no "
            "DSN was found in the environment, the keyring, the secrets file or the "
            f"vault{hit}. inspect: set M3_PRIMARY_PG_URL to a postgresql:// URL "
            "(e.g. postgresql://user:pass@host:5432/dbname) or store it in the "
            "keyring. Note: the primary store does not read PG_URL -- that is the "
            "data-warehouse DSN (now M3_CDW_PG_URL). It reads M3_PRIMARY_PG_URL, or "
            "M3_PG_URL as a fallback. Will not silently fall back to SQLite."
        )
    _reject_forbidden_host(url)
    _reject_same_as_warehouse(url)
    return url


def _dsn_identity(url: str) -> tuple[str, int, str] | None:
    """Normalize a DSN to its (host, port, dbname) identity for comparison.

    Two DSNs that name the SAME database differ byte-for-byte in benign ways
    (URI vs keyword form, trailing slash, param order, an explicit vs. implicit
    default port, or different credentials to the same DB). Comparing the parsed
    identity instead of the raw string catches those. Returns None if the DSN
    can't be parsed (caller treats an unparseable DSN as "can't prove same" — the
    forbidden-host guard and the rename remain the primary defenses). Parsing is
    m3_core.dsn's (libpq's own parser; the urlparse this replaced read a keyword
    DSN's whole text, password included, as the database name)."""
    from m3_core.dsn import parse_pg_dsn

    parts = parse_pg_dsn(url)
    return parts.identity() if parts else None


def _resolve_warehouse_dsn_for_guard() -> str:
    """Resolve the warehouse DSN the same way the warehouse consumers do —
    env (M3_CDW_PG_URL > PG_URL) AND the vault key PG_URL — so the same-DSN guard
    sees a vault-stored warehouse DSN too (the standard 'creds in keyring' setup),
    not just an env one. Best-effort; returns '' on any failure."""
    try:
        from m3_core.secret_store import PgRole, resolve_pg_dsn

        return resolve_pg_dsn(PgRole.WAREHOUSE)
    except Exception:  # noqa: BLE001 -- best-effort: the guard must not block a valid primary
        return ""


def _resolve_store_dsn(store) -> str:
    """The DSN of a named PostgreSQL store (CONFIG_DESIGN §14). ``dsn_secret`` names a
    BOOTSTRAP secret (§10): env, keyring, the secrets file, never the vault (it
    lives in a store a DSN unlocks). The primary's guards apply: a forbidden host
    and the warehouse DSN are refused, so a store can never become the warehouse."""
    from m3_core.secret_store import VaultPolicy, _env_first, explain_secret

    found = explain_secret((store.dsn_secret,), env_hit=_env_first((store.dsn_secret,)), vault=VaultPolicy.NEVER)
    if not found.value:
        tiers = ("; tiers reported: " + " | ".join(found.errors)) if found.errors else ""
        raise RuntimeError(
            f"observed: store {store.name!r} (backend postgres) names dsn_secret "
            f"{store.dsn_secret}, which is not set in the environment, the keyring or the "
            f"secrets file{tiers}. inspect: `python -m keyring set system {store.dsn_secret}` "
            f"(prompts for the DSN), or [stores.{store.name}] in m3_config.toml"
        )
    _reject_forbidden_host(found.value)
    _reject_same_as_warehouse(found.value)
    return found.value


def _reject_same_as_warehouse(primary_url: str) -> None:
    """Raise if the PRIMARY DSN names the SAME database as the WAREHOUSE DSN.

    The warehouse is a shared fan-in mirror that many instances UPSERT into; the
    primary store is one instance's authoritative DB. If they were the SAME
    database, every peer's pg_sync would overwrite another peer's live primary —
    always a misconfiguration. Compared on the normalized (host, port, dbname)
    identity (see ``_dsn_identity``): same host but a DIFFERENT database (a
    single-node dev box running both) is legitimate and allowed; only the SAME
    database is rejected. The warehouse DSN is resolved from env AND the vault so
    the standard keyring-stored setup is covered, not just env.
    """
    cdw = _resolve_warehouse_dsn_for_guard()
    if not cdw:
        return
    primary_id = _dsn_identity(primary_url)
    cdw_id = _dsn_identity(cdw)
    if primary_id is not None and primary_id == cdw_id:
        # Extract host/port/dbname for observability — safe, no credentials
        p_id = primary_id or ("?", "?", "?")
        raise RuntimeError(
            f"observed: PostgreSQL PRIMARY-store DSN names the SAME database as "
            f"the warehouse DSN (host={p_id[0]}, port={p_id[1]}, "
            f"dbname={p_id[2]}). cause: M3_PRIMARY_PG_URL and "
            f"M3_CDW_PG_URL (or PG_URL) both resolve to the same database. "
            f"The warehouse is a shared pg_sync fan-in mirror; the primary is "
            f"this instance's authoritative store — they must be different "
            f"databases. inspect: set M3_CDW_PG_URL to a different dbname "
            f"(different database on the same host is fine, or a different host "
            f"entirely). A different dbname on the same {p_id[0]}:{p_id[1]} is "
            f"sufficient."
        )


# ── Snapshot support (pg_dump / pg_restore) ──────────────────────────────────

_PG_TOOL_ENV = {"pg_dump": "M3_PG_DUMP", "pg_restore": "M3_PG_RESTORE"}

# A data block in `pg_restore --data-only` output: `COPY schema.table (cols) FROM stdin;`
# then one line per row (COPY text format escapes embedded newlines as \n, so a
# row is always exactly one line), ended by `\.`. Either name may be quoted.
_IDENT = rb'(?:"((?:[^"]|"")+)"|([^\s."]+))'
_COPY_RE = re.compile(rb"^COPY " + _IDENT + rb"\." + _IDENT + rb" \(.*\) FROM stdin;\r?$")


def _pg_tool(name: str) -> str:
    """Absolute path to a PostgreSQL client tool, or SnapshotError saying how to get one.

    ``M3_PG_DUMP`` / ``M3_PG_RESTORE`` override PATH. pg_restore is looked for
    next to an overridden pg_dump first, so the pair comes from one install.
    """
    import shutil

    explicit = os.environ.get(_PG_TOOL_ENV[name])
    if explicit:
        if os.path.isfile(Path(explicit)):
            return str(Path(explicit).absolute())
        raise SnapshotError(f"{_PG_TOOL_ENV[name]}={explicit!r} is not a file.")
    if name == "pg_restore" and (_dump := _m3cfg.setting("db.postgres.pg_dump")):
        sibling = shutil.which("pg_restore", path=str(Path(_dump).parent))
        if sibling:
            return sibling
    found = shutil.which(name)
    if found:
        return found
    raise SnapshotError(
        f"observed: {name} is not on PATH. cause: a PostgreSQL store is snapshotted "
        f"with pg_dump and verified with pg_restore; no client tools are installed "
        f"here. inspect: install the PostgreSQL client tools (same major version as "
        f"the server or newer), or point {_PG_TOOL_ENV[name]} at the executable. "
        f"Refusing to continue without a backup."
    )


def _libpq_quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _conninfo_and_env(dsn: str) -> tuple[str, dict[str, str]]:
    """A libpq conninfo WITHOUT the password, plus an env carrying it.

    Command lines are readable by every local user (``ps``, Task Manager), so
    the password goes to the child through ``PGPASSWORD`` and never through
    argv. Everything else in the DSN — sslmode, options, a socket host — is
    preserved, because those change what the tool connects to.
    """
    try:
        from psycopg2.extensions import parse_dsn

        params = dict(parse_dsn(dsn))
    except Exception as e:  # noqa: BLE001 — never echo the DSN: it carries a password
        raise SnapshotError(f"cannot parse the PostgreSQL DSN ({type(e).__name__}).") from e
    password = params.pop("password", None)
    from m3_core.config.env import secret_env_names
    from m3_core.seams import childenv

    # pg_dump / pg_restore are not m3: no m3 secret reaches them; the password
    # goes in PGPASSWORD, never on argv.
    env = childenv.child_env(drop=secret_env_names())
    if password:
        env["PGPASSWORD"] = str(password)
    return " ".join(f"{k}={_libpq_quote(v)}" for k, v in params.items()), env


def _tail(text: str | bytes, n: int = 400) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return text.strip()[-n:]


def _remove_file(path: str) -> None:
    Path(path).unlink(missing_ok=True)


def _archive_row_counts(pg_restore: str, archive: str, schema: str) -> dict[str, int]:
    """Row count per table of ``schema`` as stored IN the archive.

    Streams ``pg_restore --data-only`` to stdout and counts each COPY block's
    lines, so every data block is decompressed and read back — a truncated or
    corrupt archive fails here rather than at restore time.
    """
    import subprocess

    counts: dict[str, int] = {}
    current: str | None = None
    # stderr goes to a temp file, not a second pipe: an undrained stderr pipe
    # that fills while we read stdout deadlocks both processes.
    import tempfile

    with (
        tempfile.TemporaryFile() as errf,
        subprocess.Popen(  # nosec B603 — absolute tool path, no shell, argv list
            # `--file=-` explicitly: pg_restore 18 refuses to default to stdout.
            [pg_restore, "--data-only", "--file=-", archive],
            stdout=subprocess.PIPE,
            stderr=errf,
        ) as proc,
    ):
        assert proc.stdout is not None
        for line in proc.stdout:
            if current is None:
                m = _COPY_RE.match(line)
                if m:
                    sch = (m.group(1) or b"").replace(b'""', b'"') or m.group(2)
                    tbl = (m.group(3) or b"").replace(b'""', b'"') or m.group(4)
                    current = tbl.decode("utf-8") if sch.decode("utf-8") == schema else ""
                    if current:
                        counts.setdefault(current, 0)
            elif line.rstrip(b"\r\n") == b"\\.":
                current = None
            elif current:
                counts[current] += 1
        rc = proc.wait()
        errf.seek(0)
        stderr = errf.read()
    if rc != 0:
        raise SnapshotError(f"pg_restore could not read the archive back: {_tail(stderr)}")
    if current is not None:
        raise SnapshotError("pg_restore output ended inside a data block; the archive is truncated.")
    return counts


# ── PostgreSQL SQL dialect (co-located with the backend it belongs to) ───────
# Lives HERE, not in dialect.py (DESIGN_PHILOSOPHIES §2: one file per backend).
@dataclass(frozen=True, slots=True)
class PostgresDialect(Dialect):
    """PostgreSQL SQL surface (one-schema chatlog, %s binds)."""

    backend: BackendName = "postgres"
    param_style: ParamStyle = "format"

    @override
    def compile_keyword_query(self, query: str, mode: str) -> tuple[str, bool]:
        from .keyword import _compile_tsquery

        return _compile_tsquery(query, mode)

    @override
    def blob_type(self) -> str:
        return "BYTEA"

    @override
    def insert_or_ignore(self) -> str:
        # postgres has no verb form — the arbiter goes in a trailing clause.
        return "INSERT INTO"

    @override
    def on_conflict_ignore(self, *, conflict_target: str = "", index_predicate: str = "") -> str:
        if index_predicate and not conflict_target:
            raise ValueError("index_predicate requires a conflict_target (partial-index arbiter)")
        tgt = f" {conflict_target}" if conflict_target else ""
        pred = f" WHERE {index_predicate}" if index_predicate else ""
        return f"ON CONFLICT{tgt}{pred} DO NOTHING"

    @override
    def now(self) -> str:
        return "NOW()"

    @override
    def now_minus_days(self, days_placeholder: str) -> str:
        # %s binds an int number of days; multiply a 1-day interval.
        return f"NOW() - ({days_placeholder} * INTERVAL '1 day')"

    @override
    def now_plus_seconds(self, seconds_placeholder: str) -> str:
        # make_interval takes a bound int; `NOW() + %s * INTERVAL '1 second'`
        # would also work but multiplies a literal interval by a bind, which
        # some drivers type-infer badly.
        return f"NOW() + make_interval(secs => {seconds_placeholder})"

    @override
    def now_minus_minutes(self, minutes_placeholder: str) -> str:
        # %s binds an int number of minutes; multiply a 1-minute interval.
        return f"NOW() - ({minutes_placeholder} * INTERVAL '1 minute')"

    @override
    def age_days_gt(self, ts_column: str, days_expr: str) -> str:
        # "row older than N days": col < now - N*1day. Mirrors SQLite's
        # julianday('now')-julianday(col) > N without PG-absent julianday().
        return f"{ts_column} < NOW() - ({days_expr} * INTERVAL '1 day')"

    @override
    def age_minutes_lt(self, ts_column: str, minutes_expr: str) -> str:
        # Interval arithmetic on a real timestamptz — format-agnostic by
        # construction, same reasoning as the SQLite julianday form.
        return f"{ts_column} > NOW() - ({minutes_expr} * INTERVAL '1 minute')"

    @override
    def all_rows_after_offset(self, offset_placeholder: str) -> str:
        # PG spells "no upper bound" as LIMIT ALL (vs SQLite's LIMIT -1).
        return f"LIMIT ALL OFFSET {offset_placeholder}"

    @override
    def group_concat(self, expr: str, separator: str = ",") -> str:
        # PG's string_agg is the GROUP_CONCAT analogue. string_agg requires a text
        # arg; ids/columns used here are already TEXT, so no cast needed.
        return f"string_agg({expr}, '{separator}')"

    # SQLSTATE codes for the "object does not exist" class (class 42, syntax/access):
    #   42703 undefined_column, 42P01 undefined_table, 42704 undefined_object,
    #   42883 undefined_function. Stable across PG versions/locales — far more
    #   robust than matching the English message.
    @override
    def greatest(self, *exprs: str) -> str:
        return f"GREATEST({', '.join(exprs)})"

    @override
    def least(self, *exprs: str) -> str:
        return f"LEAST({', '.join(exprs)})"

    _UNDEFINED_SQLSTATES = frozenset({"42703", "42P01", "42704", "42883"})

    @override
    def is_undefined_object_error(self, exc: BaseException) -> bool:
        for e in (exc, getattr(exc, "__cause__", None)):
            if e is None:
                continue
            code = getattr(e, "pgcode", None)
            if code in self._UNDEFINED_SQLSTATES:
                return True
            # Fallback for a non-psycopg wrapper that lost pgcode: match the text.
            msg = str(e).lower()
            if "does not exist" in msg and ("column" in msg or "relation" in msg):
                return True
        return False

    @override
    def is_integrity_error(self, exc: BaseException) -> bool:
        # Integrity violations are SQLSTATE class 23 (23505 unique_violation,
        # 23503 foreign_key_violation, 23502 not_null_violation, 23514
        # check_violation). Match the CLASS prefix so any 23xxx counts.
        for e in (exc, getattr(exc, "__cause__", None)):
            if e is None:
                continue
            code = getattr(e, "pgcode", None)
            if isinstance(code, str) and code.startswith("23"):
                return True
        return False

    @override
    def day_bucket(self, column: str) -> str:
        # to_char keeps the TEXT 'YYYY-MM-DD' shape identical to SQLite's substr;
        # ::date would return a PG date type and change the bucket value shape.
        #
        # `AT TIME ZONE 'UTC'` pins the bucket to UTC. Without it, to_char()
        # renders a TIMESTAMPTZ in the SESSION timezone, so the same row buckets
        # into a different DAY depending on a server/connection setting, while
        # SQLite's substr() of ISO-UTC text is always UTC. Verified on PG 15.18:
        # '2026-09-07T02:30:00Z' -> '2026-09-07' under UTC but '2026-09-06'
        # under America/New_York; with the pin it is '2026-09-07' in both.
        # The two backends agreed only because this deployment happens to run
        # Etc/UTC -- agreement by configuration, not by contract.
        return f"to_char({column} AT TIME ZONE 'UTC', 'YYYY-MM-DD')"

    @override
    def byte_length(self, column: str) -> str:
        # Native on PG. NOTE: length() on PG counts CHARACTERS, not bytes, so
        # the SQLite idiom would be silently wrong here (a CJK row would report
        # ~1/3 its true byte size).
        return f"octet_length({column})"

    @override
    def has_content(self, column: str) -> str:
        return f"LENGTH(TRIM(COALESCE({column}, ''))) > 0"

    @override
    def empty_json_default(self) -> str | None:
        return "{}"  # metadata_json is JSONB; '' is rejected, '{}' is the empty obj

    @override
    def returning_id_clause(self) -> str:
        return " RETURNING id"  # no last_insert_rowid on PG; RETURNING is the way

    @override
    def claim_message(
        self,
        conn: object,
        *,
        table: str,
        where_sql: str,
        where_params: tuple,
        claimant: str,
        lease_ttl: int = 300,
    ) -> tuple | None:
        """Claim via ``FOR UPDATE SKIP LOCKED`` -- the native primitive.

        SKIP LOCKED is what SQLite cannot express: a candidate row already
        locked by another claimer is SKIPPED rather than waited on, so N
        claimers proceed in parallel instead of serializing. The SQLite path
        reaches the same guarantee by blocking, which is why these are two
        renderings and not shared SQL (see the base docstring's measurement).

        No token is needed here. The subselect locks the row it picks, so the
        UPDATE cannot collide, and RETURNING is unconditionally available on
        every supported PostgreSQL.

        Same CLAIM-THEN-RELEASE contract as every backend: this commits before
        returning and the caller must not hold a transaction across the work.
        It matters less on PG than on SQLite, but a caller written against one
        backend's forgiveness is a caller that breaks on the other.
        """
        token = str(uuid.uuid4())
        cur = conn.execute(  # type: ignore[attr-defined]
            f"UPDATE {table} SET claimed_by = %s, claimed_at = {self.now()}, "
            f"lease_token = %s, "
            f"claim_expires_at = {self.now_plus_seconds('%s')}, "
            f"attempt_count = attempt_count + 1 "
            f"WHERE id = (SELECT id FROM {table} WHERE {where_sql} "
            f"AND claimed_by IS NULL ORDER BY id LIMIT 1 "
            f"FOR UPDATE SKIP LOCKED) RETURNING id, lease_token",
            (claimant, token, lease_ttl, *where_params),
        )
        row = cur.fetchone()
        conn.commit()  # type: ignore[attr-defined]
        # (id, lease_token) -- same contract as every backend. No select-back
        # needed here: RETURNING hands both columns out of the one statement.
        return (row[0], row[1]) if row else None

    @override
    def sweep_expired_leases(
        self,
        conn: object,
        *,
        table: str,
        max_attempts: int = 5,
        limit: int = 100,
    ) -> tuple[int, int]:
        """Reclaim lapsed leases, dividing the work between concurrent sweepers.

        Same contract and same outcome as the base implementation; the
        difference is FOR UPDATE SKIP LOCKED, which lets N sweepers run at once
        without contending -- each skips rows another is already reclaiming
        instead of blocking on them. The base version is correct on SQLite
        because SQLite serializes writers anyway, so there is never a second
        concurrent sweeper to divide work with.

        LIMIT bounds each pass so a large backlog is reclaimed incrementally
        rather than in one long transaction holding locks across thousands of
        rows.
        """
        # The predicate comes from the seam, NOT a local copy. This method
        # overrides the base one only to add FOR UPDATE SKIP LOCKED; the
        # definition of "expired" must stay identical across backends, and a
        # second spelling here is how that silently stops being true (§10a).
        expired = self.lease_expired_predicate()
        dead = conn.execute(  # type: ignore[attr-defined]
            f"UPDATE {table} SET failed_at = NOW(), claimed_by = NULL, "
            f"lease_token = NULL, claim_expires_at = NULL "
            f"WHERE id IN (SELECT id FROM {table} WHERE {expired} "
            f"AND attempt_count >= %s ORDER BY claim_expires_at LIMIT %s "
            f"FOR UPDATE SKIP LOCKED)",
            (max_attempts, limit),
        ).rowcount
        back = conn.execute(  # type: ignore[attr-defined]
            f"UPDATE {table} SET claimed_by = NULL, lease_token = NULL, "
            f"claim_expires_at = NULL, claimed_at = NULL "
            f"WHERE id IN (SELECT id FROM {table} WHERE {expired} "
            f"AND attempt_count < %s ORDER BY claim_expires_at LIMIT %s "
            f"FOR UPDATE SKIP LOCKED)",
            (max_attempts, limit),
        ).rowcount
        conn.commit()  # type: ignore[attr-defined]
        return (back, dead)

    @override
    def last_insert_id(self, cursor: object) -> object:
        return cursor.fetchone()[0]  # type: ignore[attr-defined]

    @override
    def store_change_token(self, store: str) -> None:
        """No connection-free change signal; callers poll by query.

        LISTEN/NOTIFY is the upgrade path: it needs a held connection, so it
        belongs behind a backend-level watcher, not this connection-free hook.
        """
        return

    @override
    def _json_extract_text_expr(self, column: str, json_path: str) -> str:
        return f"{column} ->> '{json_path}'"

    @override
    def _json_extract_int_expr(self, column: str, json_path: str) -> str:
        return f"({column} ->> '{json_path}')::int"

    @override
    def _json_is_valid_expr(self, column: str) -> str:
        # metadata_json is JSONB here (pg_primary_v1.sql), so validity is
        # enforced by the COLUMN TYPE: Postgres parses on write and rejects
        # malformed input, and psycopg hands back a dict on read. There is no
        # such thing as a stored-but-unparseable JSONB value, which makes the
        # honest expression a constant TRUE.
        #
        # Deliberately NOT pg_input_is_valid(col::text, 'jsonb'): that is PG16+
        # only, and on a JSONB column it round-trips the value through text to
        # re-answer a question the type system already settled — cost with no
        # information. If a TEXT json column ever appears on this backend, THAT
        # is when a real check belongs here, keyed off the column's type.
        #
        # The repair built on this therefore no-ops on Postgres rather than
        # scanning, which is the correct outcome: nothing to repair.
        return "TRUE"

    @override
    def _temporal_open_clause_expr(self, column: str, op: str, p: str) -> str:
        return f"({column} IS NULL OR {column} {op} {p})"

    @override
    def coalesce_open_timestamp(self, column: str, fill_placeholder: str) -> str:
        return f"COALESCE({column}, {fill_placeholder})"

    @override
    def _table_exists_query(self, table: str) -> tuple[str, tuple]:
        # to_regclass returns NULL for a non-existent relation; the WHERE keeps the
        # result shape (0 or 1 rows) identical to the sqlite probe.
        return ("SELECT 1 WHERE to_regclass(%s) IS NOT NULL", (table,))

    @override
    def _columns_of_query(self, table: str) -> tuple[str, tuple]:
        sql = "SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position"
        return (sql, (table,))

    @override
    def _column_exists_query(self, table: str, column: str) -> tuple[str, tuple]:
        sql = "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s"
        return (sql, (table, column))

    @override
    def _qualified_table_expr(self, name: str, schema: str) -> str:
        # A separate logical store is a SCHEMA namespace in the one primary
        # database on PG (single DSN) — qualify the name so it resolves there
        # regardless of search_path. Identifiers are pre-validated in the base.
        return f"{schema}.{name}"

    @override
    def _glob_fragment(self, column: str, placeholder: str, pattern: str) -> tuple[str, str]:
        # PG has no GLOB; use case-sensitive LIKE and translate the glob wildcards
        # to LIKE ones. Escape literal % and _ (LIKE wildcards) in the SOURCE first
        # so they stay literal, THEN map glob * -> % and ? -> _.
        translated = (
            pattern.replace("\\", "\\\\")  # escape the escape char itself
            .replace("%", "\\%")
            .replace("_", "\\_")
            .replace("*", "%")
            .replace("?", "_")
        )
        return (f"{column} LIKE {placeholder}", translated)

    @override
    def compact_storage(self, *, sqlite_path: str | None = None, max_bytes: int = 500 * 1024 * 1024) -> str:
        # No client-issued compaction: autovacuum reclaims dead tuples in the
        # background, and a manual VACUUM here would need its own out-of-txn
        # connection without shrinking the files (only VACUUM FULL does, under an
        # exclusive lock — not something a routine maintenance pass should take).
        return "VACUUM skipped: not applicable on PostgreSQL (autovacuum handles this)"

    @override
    def set_statement_timeout(self, conn: object, ms: int) -> None:
        # See the SQLite sibling: int() coercion, and SET takes no bound
        # parameter either.
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(f"SET statement_timeout = {int(ms)}")


# The one shared frozen singleton for PostgreSQL.
POSTGRES = PostgresDialect()


@register_backend("postgres", dialect=POSTGRES)
class PostgresBackend:
    """Pooled PostgreSQL adapter satisfying the `StorageBackend` seam."""

    name: BackendName = "postgres"
    stores_databases_as_files = False

    @classmethod
    def from_store(cls, store) -> PostgresBackend:
        """A PostgreSQL store: its own pool on the DSN its ``dsn_secret`` names (2C.3)."""
        return cls(dsn=_resolve_store_dsn(store))

    @classmethod
    def store_cache_key(cls, store) -> str:
        return f"store:{store.name}:{store.dsn_secret}"

    def prepare_store(self) -> None:
        """The core schema (base + pending pg migrations). Nothing creates it on
        first connect, so a store on a fresh database would otherwise have only
        the files schema, which migrates lazily."""
        self.ensure_schema()

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or _resolve_dsn()
        self._pool: ThreadedConnectionPool | None = None
        self._lock = threading.Lock()
        self._caps: Capabilities | None = None
        self._schema_ready = False

    # -- schema init ---------------------------------------------------------
    def ensure_schema(self) -> None:
        """Apply the PG primary schema once, idempotently — the PG analogue of
        SQLite's lazy `_lazy_init`.

        SQLite auto-creates its schema on first `_db()` touch; a PG deployment had
        no equivalent (tables had to be made by hand via psql). This reads
        ``pg_primary_v1.sql`` (all ``CREATE TABLE/INDEX IF NOT EXISTS``, safe to
        re-run) and applies it in one transaction. Runs at most once per backend
        instance (guarded by ``_schema_ready``); the SQL's own IF NOT EXISTS makes
        a concurrent double-apply harmless.

        psycopg2 executes a multi-statement string in a single ``execute`` inside
        a real transaction — so no SQLite ``executescript``/SAVEPOINT dance is
        needed (that machinery stays SQLite-only in migrate_memory.py).
        """
        if self._schema_ready:
            return
        # Acquire the pool BEFORE taking _lock — _ensure_pool takes the same
        # (non-reentrant) lock, so nesting them would deadlock.
        pool = self._ensure_pool()
        with self._lock:
            if self._schema_ready:
                return
            from memory import config as _cfg

            sql_path = Path(_cfg.BASE_DIR) / "memory" / "migrations" / "postgres" / "pg_primary_v1.sql"
            if not os.path.exists(sql_path):
                raise RuntimeError(
                    f"observed: PostgreSQL schema file not found at {sql_path}. "
                    f"possible: an incomplete install, a packaging error, or a "
                    f"BASE_DIR pointing at the wrong tree. "
                    f"inspect: verify the file exists in the m3-memory installation; "
                    f"reinstall with `pip install m3-memory` if the file is missing. "
                    f"Cannot initialize the PostgreSQL primary schema."
                )
            schema_sql = sql_path.read_text(encoding="utf-8")
            conn = pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute(schema_sql)
                conn.commit()
                # Apply any PG-native incremental migrations (pg_NNN_*.up.sql,
                # version > the v39 baseline) — the PG analogue of SQLite's
                # migrate_memory runner. Additive schema changes past v39 land here
                # so a PG deployment doesn't drift behind the SQLite one. Each file
                # commits in its own transaction inside the runner.
                try:
                    from migrate_pg import run_pending_pg_migrations

                    applied = run_pending_pg_migrations(conn)
                    if applied:
                        import logging

                        logging.getLogger("memory.backends.postgres").info(
                            "Applied PG incremental migrations: %s", applied
                        )
                except ImportError:
                    # bin/ not importable in this context — baseline still applied.
                    pass
                self._schema_ready = True
            except Exception:
                conn.rollback()
                raise
            finally:
                pool.putconn(conn)

    # -- pool lifecycle ------------------------------------------------------
    def _ensure_pool(self) -> ThreadedConnectionPool:
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is not None:
                return self._pool
            try:
                from psycopg2.pool import ThreadedConnectionPool
            except ImportError as e:  # fail loud, actionable
                raise RuntimeError(
                    "observed: psycopg2 module is not available. "
                    "cause: the PostgreSQL driver was not installed. "
                    "inspect: install it with `pip install 'psycopg2-binary'` or "
                    "`pip install 'm3-memory[postgres]'`. The PostgreSQL backend "
                    "requires psycopg2 to connect to the database."
                ) from e
            minconn = _m3cfg.setting("db.postgres.pool_min") or _DEFAULT_MINCONN
            maxconn = _m3cfg.setting("db.postgres.pool_max") or _DEFAULT_MAXCONN
            if maxconn < minconn:
                raise ValueError(
                    f"M3_PG_POOL_MAX ({maxconn}) < M3_PG_POOL_MIN ({minconn}). "
                    f"observed: max {maxconn}, min {minconn}. inspect: M3_PG_POOL_MAX, M3_PG_POOL_MIN"
                )
            # Pin the CLIENT encoding instead of inheriting the server's.
            # psycopg2 adopts the server encoding, so against a SQL_ASCII
            # cluster every non-ASCII character m3 writes raises
            # UnicodeEncodeError inside the driver — far from the code that
            # produced the text, and for m3 that means its OWN payload: the
            # schema file and the error strings are full of em dashes, so the
            # very first schema load dies.
            #
            # How a cluster ends up SQL_ASCII without anyone choosing it
            # (measured 2026-09-30, minimal Debian 13 LXC): the image ships no
            # UTF-8 locale, so `apt install postgresql` runs initdb under
            # LC_CTYPE=C and the cluster comes up SQL_ASCII. The only hint is a
            # perl locale warning in the apt output.
            #
            # UTF-8 on the wire is correct against ANY server encoding — the
            # server transcodes, and SQL_ASCII passes bytes through — so this
            # is a portability fix, not a patch for one box. An explicit
            # client_encoding in the DSN wins: never override the operator.
            # Ask libpq's own parser whether the operator set it, rather than
            # looking for the substring: a DSN whose DATABASE is named
            # `client_encoding_notes` contains it without setting anything, and
            # the substring check then silently skipped the pin — caught by
            # test_a_dsn_mentioning_encoding_elsewhere_is_not_mistaken_for_a_choice.
            # A DSN libpq cannot parse is left to fail in connect(), where the
            # error is actionable; we still pin, since that is the safe default.
            connect_kwargs = {}
            try:
                from psycopg2.extensions import parse_dsn

                already_set = "client_encoding" in parse_dsn(self._dsn or "")
            except Exception:
                already_set = False
            if not already_set:
                connect_kwargs["client_encoding"] = "UTF8"
            self._pool = ThreadedConnectionPool(minconn, maxconn, dsn=self._dsn, **connect_kwargs)
            return self._pool

    def close(self) -> None:
        """Close all pooled connections. For clean shutdown / test teardown."""
        with self._lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None

    # -- StorageBackend surface ---------------------------------------------
    def dialect(self) -> Dialect:
        return POSTGRES

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Yield a pooled psycopg2 connection with SQLite-identical discipline.

        Commit on clean exit, rollback on exception, return to the pool always.
        This mirrors ``M3Context.get_sqlite_conn`` so call sites are backend-blind.
        """
        pool = self._ensure_pool()
        conn = pool.getconn()
        try:
            # Wrap so the memory core's sqlite-style db.execute()/row["col"] works
            # unchanged; commit/rollback operate on the raw connection underneath.
            yield _SqliteCompatConnection(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    @contextmanager
    def open_readonly(self, db_path: str) -> Iterator[Any]:
        """A READ-ONLY connection, enforced as on SQLite (``mode=ro``). On PG there
        is ONE pooled store, so ``db_path`` (a SQLite file) is ignored.

        It used to yield a normal writable connection, so the same call that
        raised "attempt to write a readonly database" on SQLite silently wrote on
        PostgreSQL, and two writers sat on it unnoticed (2026-10-10). The session
        is set read-only for the block and restored to the server default before
        the connection returns to the pool.
        """
        del db_path  # a SQLite file path; not applicable to the pooled PG store
        pool = self._ensure_pool()
        conn = pool.getconn()
        try:
            conn.rollback()  # session characteristics change only outside a transaction
            conn.set_session(readonly=True)
            try:
                yield _SqliteCompatConnection(conn)
            finally:
                conn.rollback()  # nothing to commit in a read-only session
                conn.readonly = None  # back to the server default for the pool
        finally:
            pool.putconn(conn)

    def is_query_defect(self, exc: BaseException) -> bool:
        """psycopg2's ProgrammingError covers syntax errors, unknown columns and
        bad binds; undefined_table (42P01) is excluded, matching SQLite's "no such
        table". Read from sys.modules: if psycopg2 never loaded, no exception
        here came from it."""
        import sys

        pg = sys.modules.get("psycopg2")
        if pg is None or not isinstance(exc, pg.Error):
            return False
        code = getattr(exc, "pgcode", None)
        if code == "25006":  # read_only_sql_transaction: a write through open_readonly,
            return True  # as SQLite's "attempt to write a readonly database"
        return isinstance(exc, pg.ProgrammingError) and code != "42P01"

    def placeholder(self, n: int = 1) -> str:
        """Positional binds for psycopg: ``placeholder(3) -> "%s, %s, %s"``."""
        return POSTGRES.placeholder(n)

    def list_tables(self, conn: object) -> set[str]:
        """User tables in the ACTIVE schema (current_schema(), not a hardcoded
        'public').

        Deployments that put m3 in a named schema — or set a search_path so a
        warehouse and a primary can share a cluster — would otherwise get the
        wrong answer, silently, since 'public' still exists and would just look
        empty. information_schema already excludes pg_catalog and
        information_schema themselves.
        """
        cur = conn.cursor()  # type: ignore[attr-defined]
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
        return {r[0] for r in cur.fetchall()}

    def bulk_upsert(
        self,
        conn: object,
        table: str,
        columns: list[str],
        rows: list[tuple],
        *,
        conflict_target: str,
        update_columns: list[str],
        guard_sql: str = "",
    ) -> int:
        """`execute_values`: ONE multi-row statement per page, not one per row.

        ⚠ Deliberately NOT `cursor.executemany`. psycopg2's executemany runs the
        statement once per row (see _SqliteCompatConnection.executemany's own
        docstring), so on a remote cluster a 10k-row batch becomes 10k round
        trips. execute_values expands the single ``VALUES %s`` marker into a
        multi-row VALUES list, which is why this primitive exists at all.

        page_size is bounded so one statement cannot grow past what the server
        will parse comfortably; execute_values loops internally across pages.
        """
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        col_list = ", ".join(columns)
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_columns)
        sql = f"INSERT INTO {table} ({col_list}) VALUES %s ON CONFLICT {conflict_target} DO UPDATE SET {set_clause}"
        if guard_sql:
            sql += f" {guard_sql}"
        cur = conn.cursor()  # type: ignore[attr-defined]
        execute_values(cur, sql, rows, page_size=_UPSERT_PAGE_SIZE)
        return len(rows)

    def bulk_insert_ignore(
        self,
        conn: object,
        table: str,
        columns: list[str],
        rows: list[tuple],
        *,
        conflict_target: str,
    ) -> int:
        """`ON CONFLICT ... DO NOTHING` — PostgreSQL puts the decision at the END.

        Batched with execute_values for the same reason bulk_upsert is: psycopg2's
        executemany is one round trip per row.
        """
        if not rows:
            return 0
        from psycopg2.extras import execute_values

        d = self.dialect()
        sql = (
            f"{d.insert_or_ignore()} {table} ({', '.join(columns)}) VALUES %s "
            f"{d.on_conflict_ignore(conflict_target=conflict_target)}"
        )
        cur = conn.cursor()  # type: ignore[attr-defined]
        execute_values(cur, sql, rows, page_size=_UPSERT_PAGE_SIZE)
        return len(rows)

    def maintenance_checkpoint(self, conn: object, *, final: bool = False) -> None:
        """No-op: PostgreSQL manages its own WAL (checkpointer + bgwriter).

        SQLite's ``PRAGMA wal_checkpoint`` has no Postgres analogue a client
        should invoke — the server's checkpointer handles it, and ``CHECKPOINT``
        is a superuser-ish server-wide operation that a batch writer has no
        business issuing. Present so callers can checkpoint on a cadence without
        knowing the backend (§1 storage seam).
        """
        return

    def store_identity(self) -> str:
        """``postgres:host:port/dbname`` — core and chatlog share it (one database)."""
        ident = _dsn_identity(self._dsn or "")
        if ident is None:
            # Unparseable: still stable and credential-free.
            # Through the FIPS boundary: a DSN can carry a password.
            from crypto_provider import get_sha256

            return "postgres:" + get_sha256((self._dsn or "").encode())[:16]
        host, port, db = ident
        return f"postgres:{host}:{port}/{db}"

    def _run_pg_dump(self, argv: list[str], env: dict[str, str]) -> None:
        """Run pg_dump. A separate method so a test can act between the
        snapshot export and the dump (the window a concurrent writer uses)."""
        import subprocess

        proc = subprocess.run(argv, env=env, capture_output=True)  # nosec B603 — absolute tool path, no shell
        if proc.returncode != 0:
            raise SnapshotError(f"pg_dump failed (rc {proc.returncode}): {_tail(proc.stderr)}")

    def snapshot(self, dest_dir: str, *, label: str) -> Snapshot:
        """``pg_dump -Fc`` pinned to an exported snapshot, verified by reading it back.

        A DEDICATED connection holds the REPEATABLE READ transaction, not a
        pooled one: it stays open for the whole dump, and parking a pool slot
        for that long starves every other caller. Row counts are read in that
        transaction and
        pg_dump attaches to its snapshot (``--snapshot``), so both see one state
        no matter what commits meanwhile.

        Scope is ``current_schema()`` — the same tables ``list_tables`` reports
        — so what is counted is exactly what is dumped and verified.
        """
        import psycopg2
        from psycopg2 import sql

        pg_dump = _pg_tool("pg_dump")
        pg_restore = _pg_tool("pg_restore")
        conninfo, env = _conninfo_and_env(self._dsn or "")
        ident = _dsn_identity(self._dsn or "")
        dst = snapshot_target(dest_dir, (ident[2] if ident else "") or "postgres", label, ".dump")
        partial = dst + ".partial"

        try:
            conn = psycopg2.connect(self._dsn)
        except Exception as e:  # noqa: BLE001 — psycopg2 errors may quote the DSN
            raise SnapshotError(f"cannot connect for the snapshot ({type(e).__name__}).") from e
        try:
            conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
            cur = conn.cursor()
            cur.execute("SELECT pg_export_snapshot(), current_schema()")
            snap_id, schema = cur.fetchone()
            expected: dict[str, int] = {}
            for t in sorted(self.list_tables(conn)):
                cur.execute(sql.SQL("SELECT COUNT(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(t)))
                expected[t] = int(cur.fetchone()[0])
            self._run_pg_dump(
                [
                    pg_dump,
                    "--format=custom",
                    f"--snapshot={snap_id}",
                    "--schema=" + '"' + schema.replace('"', '""') + '"',
                    "--no-password",
                    f"--file={partial}",
                    f"--dbname={conninfo}",
                ],
                env,
            )
        except SnapshotError:
            _remove_file(partial)
            raise
        except Exception as e:  # noqa: BLE001
            _remove_file(partial)
            raise SnapshotError(f"snapshot failed: {type(e).__name__}: {_tail(str(e))}") from e
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

        try:
            got = _archive_row_counts(pg_restore, partial, schema)
        except SnapshotError:
            _remove_file(partial)
            raise
        diffs = sorted(t for t in set(expected) | set(got) if expected.get(t, 0) != got.get(t, 0))
        if diffs:
            _remove_file(partial)
            raise SnapshotError(f"archive did not verify: row-count mismatch in {diffs[:10]}")
        Path(partial).replace(dst)
        return Snapshot(
            path=dst,
            store=self.store_identity(),
            row_counts=expected,
            verified=f"archive read back; row counts match in {len(expected)} tables",
        )

    def schema_version(self) -> int | None:
        """MAX(version) from schema_versions, or None if the table is absent."""
        with self.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('public.schema_versions')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute("SELECT MAX(version) FROM schema_versions")
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None

    def keyword_search(
        self,
        conn: object,
        query: str,
        *,
        limit: int,
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
        table: str = "memory_items",
    ) -> list[KeywordHit]:
        """tsvector keyword search — the Postgres analogue of SQLite FTS5+bm25.

        Compiles the query to a tsquery (same sanitization pipeline as the FTS5
        path), matches the generated ``search_vector`` column with ``@@``, and
        ranks with ``ts_rank``. ts_rank is higher-is-better, but the seam
        contract is LOWER-is-better (bm25 convention), so the score is NEGATED —
        callers sort ascending identically on both backends. Empty compile -> [].

        ``tenancy_sql`` must already be in this backend's ``%s`` placeholder style.
        ``table`` is the items table to search — ``memory_items`` for core (the
        default) or ``chat_log_items`` for the chatlog store (both carry the
        generated ``search_vector`` column). It is a trusted internal identifier
        (from ``chatlog_table()``), never end-user input; validated as a bare
        identifier for defense-in-depth since it is interpolated, not bound.
        """
        if not table.isidentifier():
            raise ValueError(f"table must be a bare identifier: {table!r}")
        _compile_tsquery = self.dialect().compile_keyword_query

        tsquery, ok = _compile_tsquery(query, "fts5")
        if not ok or not tsquery:
            return []
        cur = conn.cursor()  # type: ignore[attr-defined]
        # to_tsquery parses the compiled string; bind it as a parameter. Negate
        # ts_rank so lower = more relevant (matches the bm25 seam convention).
        cur.execute(
            f"""
            SELECT mi.id AS id,
                   -ts_rank(mi.search_vector, to_tsquery('english', %s)) AS score
            FROM {table} mi
            WHERE mi.search_vector @@ to_tsquery('english', %s)
              AND mi.is_deleted = 0{tenancy_sql}
            ORDER BY score ASC
            LIMIT %s
            """,
            (tsquery, tsquery, *tenancy_params, limit),
        )
        return [KeywordHit(memory_id=r[0], score=float(r[1])) for r in cur.fetchall()]

    def keyword_search_with_row_data(
        self,
        conn: object,
        query: str,
        *,
        limit: int,
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
        table: str = "memory_items",
        extra_columns: tuple = (),
        search_mode: str = "fts5",
    ) -> list[dict]:
        """tsvector keyword search projecting the row body (see base contract).

        The SAME tsquery + ts_rank query as :meth:`keyword_search`, selecting the
        row columns instead of just the id. Gives PostgreSQL the behaviours that
        were previously SQLite-only because their SQL sat inline in
        memory/search.py: the exact-substring short-circuit and the
        embedder-down keyword fallback. A PG user with a dead embedder now gets
        keyword results instead of nothing.

        ``metadata_json`` is re-serialised to TEXT when present: psycopg returns
        JSONB as a dict, while the SQLite path holds a string and downstream code
        does substring checks on it. Normalising here keeps the CALLER identical
        on both backends, which is the whole point of the seam. The ts_rank score
        is not returned — row ORDER carries the ranking, and bm25/ts_rank are not
        comparable scales (see :class:`KeywordHit`).
        """
        if not table.isidentifier():
            raise ValueError(f"table must be a bare identifier: {table!r}")
        _compile_tsquery = self.dialect().compile_keyword_query

        tsquery, ok = _compile_tsquery(query, search_mode)
        if not ok or not tsquery:
            return []
        safe_extra = [c for c in extra_columns if str(c).isidentifier()]
        extra_sql = "".join(f", mi.{c}" for c in safe_extra)
        cur = conn.cursor()  # type: ignore[attr-defined]
        cur.execute(
            f"""
            SELECT mi.id, mi.content, mi.title, mi.type, mi.importance{extra_sql},
                   -ts_rank(mi.search_vector, to_tsquery('english', %s)) AS _rank
            FROM {table} mi
            WHERE mi.search_vector @@ to_tsquery('english', %s)
              AND mi.is_deleted = 0{tenancy_sql}
            ORDER BY _rank ASC
            LIMIT %s
            """,
            (tsquery, tsquery, *tenancy_params, limit),
        )
        colnames = [d[0] for d in cur.description]
        out = []
        for raw in cur.fetchall():
            row = dict(zip(colnames, raw, strict=True))
            row.pop("_rank", None)
            meta = row.get("metadata_json")
            if meta is not None and not isinstance(meta, str):
                import json

                row["metadata_json"] = json.dumps(meta)
            out.append(row)
        return out

    def vector_search(
        self,
        conn: object,
        query_vector: list,
        *,
        limit: int,
        dim: int,
        embed_models: tuple = (),
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
    ) -> list[VectorHit]:
        """Vector search, dispatched by capability to the best available path.

        The result SHAPE is identical regardless of arm (base.py invariant). Today
        only the add-on-free baseline exists; the ``if caps.has("pgvector")`` fork
        is the declared SEAM POINT so a future pgvector/HNSW ANN arm is a NEW ARM
        in this file, not a signature change across the seam (§1: CPU-only floor
        never regresses; pgvector is a Phase-4 opt-in behind the probe).
        """
        if self._detect_vector_accelerator(conn):
            # Placeholder for the pgvector ANN arm (Phase-4 opt-in). It MUST return
            # the same list[VectorHit] shape as the baseline. Until implemented,
            # fall through to the always-correct baseline (no behavior change).
            # Probes the CALLER'S conn, never a fresh pooled connection.
            pass
        return self._vector_search_baseline(
            conn,
            query_vector,
            limit=limit,
            dim=dim,
            embed_models=embed_models,
            tenancy_sql=tenancy_sql,
            tenancy_params=tenancy_params,
        )

    def _vector_search_baseline(
        self,
        conn: object,
        query_vector: list,
        *,
        limit: int,
        dim: int,
        embed_models: tuple = (),
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
    ) -> list[VectorHit]:
        """The extension-free arm: BYTEA embeddings + Rust cosine (no pgvector).

        Identical to the SQLite baseline except for placeholder style and that
        BYTEA comes back as memoryview/bytes (normalized in the shared scorer).
        Works on vanilla, locked-down federal Postgres — the CPU-only floor the
        conformance test asserts.
        """
        from ._vector import score_and_rank

        d = self.dialect()
        params: list = []
        model_sql = ""
        if embed_models:
            model_sql = f" AND me.embed_model IN ({d.placeholder(len(embed_models))})"
            params.extend(embed_models)
        params.extend(tenancy_params)
        cur = conn.cursor()  # type: ignore[attr-defined]
        cur.execute(
            f"""
            SELECT mi.id AS id, me.embedding AS embedding
            FROM memory_items mi
            JOIN memory_embeddings me ON mi.id = me.memory_id
            WHERE mi.is_deleted = 0 AND me.dim = {d.param()}{model_sql}{tenancy_sql}
            """,
            (dim, *params),
        )
        rows = [(r[0], r[1]) for r in cur.fetchall()]
        return score_and_rank(query_vector, rows, dim, limit)

    def capabilities(self) -> Capabilities:
        """Probe optional accelerators; baseline (tsvector + Rust cosine) always holds.

        pgvector is detected but NOT required — absence means the BYTEA + Rust
        cosine baseline, which is always correct. Never raises from capability
        discovery (a missing extension is normal on locked-down federal PG).
        """
        if self._caps is not None:
            return self._caps
        vector_accel = "none"
        try:
            with self.connection() as conn:
                if self._detect_vector_accelerator(conn):
                    vector_accel = "pgvector"
        except Exception:
            vector_accel = "none"
        self._caps = Capabilities(
            backend="postgres",
            keyword="tsvector",
            vector_accelerator=vector_accel,  # type: ignore[arg-type]
        )
        return self._caps

    @staticmethod
    def _detect_vector_accelerator(conn: object) -> bool:
        """True iff pgvector is installed, probed on the GIVEN connection (never
        opens a new pooled connection — so ``vector_search``, which already holds
        ``conn``, doesn't borrow a second one and risk starving a small pool).
        Never raises: any failure means the always-correct BYTEA+Rust floor.
        """
        try:
            cur = conn.cursor()  # type: ignore[attr-defined]
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            return cur.fetchone() is not None
        except Exception:  # noqa: BLE001 — capability probe; absent means the portable path
            return False
