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

import logging
import os
import re
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

from m3_core.config import settings as _m3cfg

from .base import (
    EMBEDDING_TABLES,
    BackendName,
    BackendNotConfigured,
    Capabilities,
    EmbeddingConflict,
    EmbeddingRow,
    KeywordHit,
    Snapshot,
    SnapshotError,
    VectorCopyStatus,
    VectorHit,
    embedding_row_groups,
    embedding_table,
    snapshot_target,
)
from .dialect import Dialect, ParamStyle
from .registry import register_backend

logger = logging.getLogger("memory.backends.postgres")

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
# bounded amount of work. Overridable for tuning against a slow link. Read per
# call: a value read at import ignored a later --set or config change.
def _upsert_page_size() -> int:
    return int(_m3cfg.setting("db.postgres.upsert_page_size"))


# The migration that creates the vector copy; ensure_vector_copy re-applies it.
_VECTOR_COPY_MIGRATION = 66
# Session advisory lock serialising the primary schema chain across processes.
_PRIMARY_MIGRATION_LOCK = "m3_primary_migrations"
# "This row lacks its copy". Must stay textually identical to the WHERE of
# pg_066's partial index, or the planner cannot use the index and each check
# that reads it becomes a full scan.
_LACKS_COPY = "embedding_vec IS NULL"


def _has_vec_column(cur: Any, table: str) -> bool:
    """Whether ``table`` as this session resolves it (its search_path) has pg_066's
    ``embedding_vec`` column. Read on every write rather than cached, so it is
    never stale after the extension is added or the migration is reversed, nor
    wrong for a second schema on the same DSN. Cost: one round trip per write
    call (measured 0.24 ms against 0.18 ms for ``SELECT 1`` on a local server);
    batched writers pay it once per batch."""
    cur.execute(
        "SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s) AND attname = 'embedding_vec' AND NOT attisdropped",
        (table,),
    )
    return cur.fetchone() is not None


def _vector_copy_ready(cur: Any) -> bool:
    """Whether search may score in the database: the core table has the copy
    column and no row lacks a copy (pg_066's partial index makes this cheap)."""
    if not _has_vec_column(cur, "memory_embeddings"):
        return False
    cur.execute(f"SELECT NOT EXISTS (SELECT 1 FROM memory_embeddings WHERE {_LACKS_COPY})")
    return bool(cur.fetchone()[0])


def _vector_copy_status(cur: Any) -> VectorCopyStatus:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector'), "
        "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
    )
    available, installed = cur.fetchone()
    missing: dict[str, int] = {}
    for table in sorted(EMBEDDING_TABLES):
        if _has_vec_column(cur, table):
            cur.execute(f"SELECT count(*) FROM {table} WHERE {_LACKS_COPY}")
            missing[table] = int(cur.fetchone()[0])
    return VectorCopyStatus(bool(available), bool(installed), missing)


def _vector_literal(blob: bytes | None) -> str | None:
    """pgvector's text form of a packed float32 blob. ``repr`` of each float32
    widened to a double is exact, and pgvector parses it back to the same float4,
    so the copy equals the blob bit for bit."""
    if blob is None:
        return None
    from embedding_utils import unpack

    return "[" + ",".join(map(repr, unpack(blob))) + "]"


def insert_embeddings_on(
    cur: Any,
    rows: Sequence[EmbeddingRow],
    *,
    on_conflict: EmbeddingConflict = "error",
    table: str = "memory_embeddings",
) -> int:
    """Write embedding rows through ``cur``, a cursor on a PostgreSQL database.

    BYTEA always; with pgvector (pg_066's column present) also the
    ``embedding_vec`` copy, so the row is searchable in the database the moment
    it lands. Batched with execute_values (one statement per page, not one round
    trip per row). A module function, not only the backend method, because
    pg_sync writes to a warehouse through a connection of its own and has no
    backend instance for it.
    """
    if not rows:
        return 0
    from psycopg2.extras import execute_values

    table = embedding_table(table)
    with_vec = _has_vec_column(cur, table)
    for cols, vals in embedding_row_groups(rows):
        marks = ["%s"] * len(cols)
        if with_vec:
            vals = [(*v, _vector_literal(v[2])) for v in vals]
            cols = [*cols, "embedding_vec"]
            marks.append("%s::vector")
        if on_conflict == "ignore":
            tail = POSTGRES.on_conflict_ignore(conflict_target="(id)")
        elif on_conflict == "update":
            tail = POSTGRES.on_conflict_update("(id)", cols[1:])
        else:
            tail = ""
        execute_values(
            cur,
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s {tail}".rstrip(),
            vals,
            template="(" + ", ".join(marks) + ")",
            page_size=_upsert_page_size(),
        )
    return len(rows)


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
    # libpq takes a comma-separated host list and tries each: any of them counts,
    # as does the hostaddr it dials in place of a host name.
    parsed_hosts = (
        {h.strip().lower() for h in f"{parts.host},{parts.hostaddr or ''}".split(",")} - {""} if parts else set()
    )
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

    The secrets chain under the PRIMARY role's name only (``M3_PRIMARY_PG_URL``:
    the secrets file, the keyring, the vault as a deprecated fallback), never
    the environment and never the warehouse DSN. The resolved DSN is then
    checked against ``M3_PG_FORBIDDEN_HOSTS`` (the known warehouse host(s)). No
    default — selecting the postgres backend without a DSN is a configuration
    error, not a reason to silently do something else.
    """
    from m3_core.secret_store import PgRole, explain_pg_dsn, resolve_pg_dsn

    found = explain_pg_dsn(PgRole.PRIMARY)
    url = found.dsn
    if not url:
        # Someone with the warehouse address stored may expect m3 to use it.
        wh = (
            " The warehouse address (M3_CDW_PG_URL) is stored, but it is never used for the primary store."
            if resolve_pg_dsn(PgRole.WAREHOUSE)
            else ""
        )
        # What each tier hit, not just "not set": a broken keyring or vault used
        # to be swallowed here and reported as a missing DSN.
        hit = (" Lookup notes: " + " | ".join(found.errors)) if found.errors else ""
        raise BackendNotConfigured(
            "m3 is set to use PostgreSQL, but no database address is stored "
            "(M3_PRIMARY_PG_URL: not in the secrets file, the keyring or the vault). "
            "fix: `m3 secret set M3_PRIMARY_PG_URL` (asks for the postgresql:// address), "
            "or go back to SQLite: `m3 config set db.backend sqlite`. "
            f"m3 will not switch back to SQLite on its own.{wh}{hit}"
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
    """Resolve the warehouse DSN the same way the warehouse consumers do (the
    secrets chain: file, keyring, vault), so the same-DSN guard sees it wherever
    it is stored. Returns '' when none is stored, or (with a WARNING) when the
    lookup itself failed: a primary is not refused over a broken keyring, but the
    operator is told the same-database guard did not run."""
    try:
        from m3_core.secret_store import PgRole, resolve_pg_dsn

        return resolve_pg_dsn(PgRole.WAREHOUSE)
    except Exception as e:  # noqa: BLE001 -- reported; the guard is skipped, not the primary
        logger.warning(
            f"could not read the warehouse DSN to check that the primary store is not the warehouse "
            f"({type(e).__name__}: {e}); that check was SKIPPED for this connection. "
            "inspect: m3 doctor (secrets file / keyring health)"
        )
        return ""


def _resolve_store_dsn(store) -> str:
    """The DSN of a named PostgreSQL store. ``dsn_secret`` names a
    BOOTSTRAP secret (§10): the secrets file, then the keyring, never the vault (it
    lives in a store a DSN unlocks), never the environment. The primary's guards apply: a forbidden host
    and the warehouse DSN are refused, so a store can never become the warehouse."""
    from m3_core.secret_store import VaultPolicy, explain_secret

    found = explain_secret((store.dsn_secret,), vault=VaultPolicy.NEVER, env_names=(store.dsn_secret,))
    if not found.value:
        tiers = ("; tiers reported: " + " | ".join(found.errors)) if found.errors else ""
        raise RuntimeError(
            f"observed: store {store.name!r} (backend postgres) names dsn_secret "
            f"{store.dsn_secret}, which is in neither the secrets file nor the keyring{tiers}. "
            f"fix: `m3 secret set {store.dsn_secret}` (prompts for the DSN). "
            f"inspect: [stores.{store.name}] in m3_config.toml"
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
    from m3_core.dsn import parse_pg_dsn

    primary_parts, cdw_parts = parse_pg_dsn(primary_url), parse_pg_dsn(cdw)
    primary_id = primary_parts.identity() if primary_parts else None
    # "Cannot prove different" is a refusal: a partial identity (no dbname, a
    # socket vs localhost spelling) used to compare unequal and pass.
    if primary_parts is not None and cdw_parts is not None and primary_parts.may_be_same_database(cdw_parts):
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


# A data block in `pg_restore --data-only` output: `COPY schema.table (cols) FROM stdin;`
# then one line per row (COPY text format escapes embedded newlines as \n, so a
# row is always exactly one line), ended by `\.`. Either name may be quoted.
_IDENT = rb'(?:"((?:[^"]|"")+)"|([^\s."]+))'
_COPY_RE = re.compile(rb"^COPY " + _IDENT + rb"\." + _IDENT + rb" \(.*\) FROM stdin;\r?$")


def _pg_tool(name: str) -> str:
    """Absolute path to a PostgreSQL client tool, or SnapshotError saying how to get one.

    ``db.postgres.pg_dump`` / ``db.postgres.pg_restore`` (legacy M3_PG_DUMP /
    M3_PG_RESTORE) override PATH. pg_restore is looked for next to an overridden
    pg_dump first, so the pair comes from one install.
    """
    import shutil

    explicit = _m3cfg.setting(f"db.postgres.{name}")  # a path key: parse_path
    if explicit:
        tool = Path(str(explicit))
        if os.path.isfile(tool):
            return str(tool.absolute())
        raise SnapshotError(
            f"observed: db.postgres.{name} = {str(explicit)!r} (from {_m3cfg.origin_of(f'db.postgres.{name}')}) "
            f"is not a file. fix: point it at the {name} executable, or remove it to search PATH."
        )
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
        f"the server or newer), or set db.postgres.{name} in m3_config.toml to the executable. "
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
    def autoincrement_id_column(self, name: str = "id") -> str:
        return f"{name} SERIAL PRIMARY KEY"

    @override
    def timestamp_type(self) -> str:
        return "TIMESTAMP WITH TIME ZONE"

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
    def _temporal_set_expr(self, column: str) -> str:
        return f"{column} IS NOT NULL"

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
        """A PostgreSQL store: its own pool on the DSN its ``dsn_secret`` names."""
        return cls(dsn=_resolve_store_dsn(store))

    @classmethod
    def store_cache_key(cls, store) -> str:
        return f"store:{store.name}:{store.dsn_secret}"

    @classmethod
    def not_configured(cls) -> str | None:
        """The database address, when none is stored or it is refused (the
        warehouse's host or database). Reads the secret; does not connect."""
        try:
            _resolve_dsn()
        except RuntimeError as e:  # BackendNotConfigured, or a refused address
            return str(e)
        return None

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
        self.migrations_applied: list[int] = []
        # Set by on_selected() for the store m3 serves; see connection().
        self._migrate_on_first_use = False

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
        fill_after = False
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
            locked = False
            try:
                cur = conn.cursor()
                # Every m3 process migrates on first use, so after an upgrade the
                # MCP server, the cognitive loop and a hook can arrive together.
                # One at a time (a session lock, held across the per-file commits);
                # the others then find nothing pending.
                cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (_PRIMARY_MIGRATION_LOCK,))
                locked = True
                # A pooled session may carry a search_path someone set and left;
                # the schema goes where the connection string put it.
                cur.execute("RESET search_path")
                cur.execute(schema_sql)
                conn.commit()
                # Apply any PG-native incremental migrations (pg_NNN_*.up.sql,
                # version > the v39 baseline) — the PG analogue of SQLite's
                # migrate_memory runner. Additive schema changes past v39 land here
                # so a PG deployment doesn't drift behind the SQLite one. Each file
                # commits in its own transaction inside the runner.
                try:
                    from migrate_pg import run_pending_pg_migrations
                except ImportError as e:
                    raise RuntimeError(
                        f"observed: the PostgreSQL migration runner (bin/migrate_pg.py) is not "
                        f"importable ({e}); the store's schema cannot be brought current, so m3 "
                        f"refuses to run on it. possible: an incomplete install. fix: reinstall "
                        f"m3-memory (`m3 upgrade`), then `m3 doctor --fix`"
                    ) from e
                applied = run_pending_pg_migrations(conn)
                self.migrations_applied = list(applied)
                if applied:
                    import logging

                    logging.getLogger("memory.backends.postgres").info("Applied PG incremental migrations: %s", applied)
                # pg_066 just added the vector copy: rows written before it
                # have none. Checked here, so a fresh store starts no thread.
                if _VECTOR_COPY_MIGRATION in applied:
                    status = _vector_copy_status(conn.cursor())
                    conn.commit()
                    fill_after = sum(status.missing.values()) > 0
                self._schema_ready = True
            except Exception:
                conn.rollback()
                raise
            finally:
                unlocked = True
                if locked:
                    try:
                        conn.rollback()
                        conn.cursor().execute("SELECT pg_advisory_unlock(hashtext(%s))", (_PRIMARY_MIGRATION_LOCK,))
                        conn.commit()
                    except Exception as e:  # noqa: BLE001 — handled below: the session is closed
                        unlocked = False
                        logger.warning(
                            f"could not release the schema-migration advisory lock on {self.store_identity()} "
                            f"({type(e).__name__}: {e}); closing that connection so the lock ends with its "
                            "session instead of staying held by a pooled connection"
                        )
                # A pooled session still holding the session lock blocked every
                # other process's schema check until this pool closed.
                pool.putconn(conn, close=not unlocked)
        if fill_after:
            # Outside the lock: the thread's own connection() call re-enters
            # ensure_schema, which now returns at once.
            self._start_vector_copy_fill()

    def _start_vector_copy_fill(self) -> None:
        """Fill the vector copy of pre-pg_066 rows in the background, right after
        the migration, so in-database search is available without waiting for
        the cognitive loop.

        A daemon thread, committing per batch: a short-lived process that exits
        first keeps the batches already done, and the cognitive loop or
        ``m3 doctor --fix`` finishes the rest. Search is correct throughout; it
        uses the baseline until every row has its copy."""
        import logging

        log = logging.getLogger("memory.backends.postgres")

        def _run() -> None:
            try:
                from .base import fill_vector_copies

                with self.connection() as conn:
                    filled = fill_vector_copies(self, conn)
                    ready = _vector_copy_ready(conn.cursor())
                    conn.commit()
                log.info(
                    "pg_066 background fill: %d vector copies written (%s); %s",
                    sum(filled.values()),
                    ", ".join(f"{t}: {n}" for t, n in sorted(filled.items())) or "none",
                    "every row has its copy, so vector search scores in the database"
                    if ready
                    else "rows still lack a copy (written meanwhile by an older client?); "
                    "search stays on the baseline until `m3 doctor --fix` or the cognitive loop fills them",
                )
            except Exception as e:  # noqa: BLE001 — a background fill must never take the process down
                log.warning(
                    "observed: pg_066 background vector copy fill stopped (%s: %s) on %s; batches "
                    "committed before it are kept and search stays correct on the baseline. "
                    "fix: `m3 doctor --fix`, or let the cognitive loop's embed pass finish it",
                    type(e).__name__,
                    e,
                    self.store_identity(),
                )

        threading.Thread(target=_run, name="m3-vector-copy-fill", daemon=True).start()

    # -- pool lifecycle ------------------------------------------------------
    def _ensure_pool(self) -> ThreadedConnectionPool:
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is not None:
                return self._pool
            try:
                from psycopg2.pool import ThreadedConnectionPool  # noqa: F401 — availability probe; the pool comes from pg_pool
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
                    f"db.postgres.pool_max ({maxconn}) < db.postgres.pool_min ({minconn}). "
                    f"observed: max {maxconn}, min {minconn}. fix: `m3 config set db.postgres.pool_max N` "
                    "(or pool_min); legacy env names M3_PG_POOL_MAX / M3_PG_POOL_MIN"
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
            from m3_core.pgconn import pg_pool  # FIPS transport check, one owner

            self._pool = pg_pool(minconn, maxconn, self._dsn, **connect_kwargs)
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
        For the store m3 serves (``on_selected``), the first connection brings the
        schema current, so an upgraded store is migrated on first use.
        """
        if self._migrate_on_first_use:
            self.ensure_schema()
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
        if self._migrate_on_first_use:
            self.ensure_schema()
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
        execute_values(cur, sql, rows, page_size=_upsert_page_size())
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
        execute_values(cur, sql, rows, page_size=_upsert_page_size())
        return len(rows)

    def insert_embeddings(
        self,
        conn: object,
        rows: Sequence[EmbeddingRow],
        *,
        on_conflict: EmbeddingConflict = "error",
        table: str = "memory_embeddings",
    ) -> int:
        """BYTEA always; with pgvector also the ``embedding_vec`` copy. See
        :func:`insert_embeddings_on`, which this delegates to."""
        cur = conn.cursor()  # type: ignore[attr-defined]
        return insert_embeddings_on(cur, rows, on_conflict=on_conflict, table=table)

    def vector_copy_status(self, conn: object) -> VectorCopyStatus | None:
        """pgvector's state here, and per embedding table with the copy column,
        the rows still lacking a copy (pg_066's partial index answers that)."""
        return _vector_copy_status(conn.cursor())  # type: ignore[attr-defined]

    def on_selected(self) -> None:
        """The store m3 serves migrates on first use (an upgraded store reaches
        the current schema without a separate step, as SQLite's does)."""
        self._migrate_on_first_use = True

    def schema_status(self, conn: object) -> tuple[int, int]:
        """(applied, known): the store's highest applied migration, and the highest
        this release ships. Applied below known means the store is behind."""
        from migrate_pg import discover_migrations

        cur = conn.cursor()  # type: ignore[attr-defined]
        cur.execute("SELECT COALESCE(max(version), 0) FROM schema_versions")
        applied = int(cur.fetchone()[0])
        return applied, max(discover_migrations() or {0: None})

    def ensure_vector_copy(self, conn: object) -> bool:
        """Re-apply pg_066's body when it has something to do: pgvector offered but
        not created (the role may have the right now), or an embedding table
        without the copy column. The body is idempotent.

        The chain comes first: re-applying pg_066's body to a store whose earlier
        migrations never ran would give it pg_066 without the ones before it."""
        self.ensure_schema()
        cur = conn.cursor()  # type: ignore[attr-defined]
        status = _vector_copy_status(cur)
        cur.execute(
            "SELECT to_regclass('memory_embeddings') IS NOT NULL, to_regclass('chat_log_embeddings') IS NOT NULL"
        )
        core, clone = cur.fetchone()
        tables = [t for t, present in (("memory_embeddings", core), ("chat_log_embeddings", clone)) if present]
        lacking = [t for t in tables if t not in status.missing]
        if (status.available and not status.installed) or (status.installed and lacking):
            from migrate_pg import discover_migrations

            cur.execute(Path(discover_migrations()[_VECTOR_COPY_MIGRATION]["up"]).read_text(encoding="utf-8"))
        return _has_vec_column(cur, "memory_embeddings")

    def fill_vector_copy(self, conn: object, *, table: str = "memory_embeddings", limit: int = 1000) -> int:
        """One batch: read up to ``limit`` blobs lacking a copy, write their copies
        in one statement. Changing only the copy, so pg_066's trigger keeps it."""
        from psycopg2.extras import execute_values

        table = embedding_table(table)
        cur = conn.cursor()  # type: ignore[attr-defined]
        if not _has_vec_column(cur, table):
            return 0
        cur.execute(f"SELECT id, embedding FROM {table} WHERE {_LACKS_COPY} LIMIT %s", (limit,))
        rows = cur.fetchall()
        if not rows:
            return 0
        execute_values(
            cur,
            f"UPDATE {table} AS t SET embedding_vec = v.vec::vector FROM (VALUES %s) AS v(id, vec) WHERE t.id = v.id",
            [(r[0], _vector_literal(bytes(r[1]))) for r in rows],
            page_size=_upsert_page_size(),
        )
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
        from psycopg2 import sql

        pg_dump = _pg_tool("pg_dump")
        pg_restore = _pg_tool("pg_restore")
        conninfo, env = _conninfo_and_env(self._dsn or "")
        ident = _dsn_identity(self._dsn or "")
        dst = snapshot_target(dest_dir, (ident[2] if ident else "") or "postgres", label, ".dump")
        partial = dst + ".partial"

        try:
            from m3_core.pgconn import pg_connect  # FIPS transport check, one owner

            conn = pg_connect(self._dsn)
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
                    "--schema=" + self.dialect().quote_identifier(schema),
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
            # Unqualified, like the read below: both resolve through the
            # search_path, so a store in a named schema is not reported absent.
            cur.execute("SELECT to_regclass('schema_versions')")
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
        as_of: str = "",
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
              AND {self.dialect().live_predicate("mi", as_of)}{tenancy_sql}
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
        as_of: str = "",
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
              AND {self.dialect().live_predicate("mi", as_of)}{tenancy_sql}
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
        as_of: str = "",
    ) -> list[VectorHit]:
        """Vector search, dispatched by capability to the best available path.

        Both arms are EXACT (every stored vector is scored) and return the same
        list[VectorHit]. With pgvector and a complete vector copy (pg_066, every
        row filled), PostgreSQL scores in the database and sends back only the
        top ``limit``; otherwise the baseline ships the candidate blobs to the
        Rust scorer. "Complete" matters: a row without its copy would be missed,
        so one missing copy anywhere sends the search to the baseline.
        """
        cur = conn.cursor()  # type: ignore[attr-defined]
        if _vector_copy_ready(cur):
            return self._vector_search_pgvector(
                cur,
                query_vector,
                limit=limit,
                dim=dim,
                embed_models=embed_models,
                tenancy_sql=tenancy_sql,
                tenancy_params=tenancy_params,
                as_of=as_of,
            )
        return self._vector_search_baseline(
            conn,
            query_vector,
            limit=limit,
            dim=dim,
            embed_models=embed_models,
            tenancy_sql=tenancy_sql,
            tenancy_params=tenancy_params,
            as_of=as_of,
        )

    def _vector_search_pgvector(
        self,
        cur: Any,
        query_vector: list,
        *,
        limit: int,
        dim: int,
        embed_models: tuple = (),
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
        as_of: str = "",
    ) -> list[VectorHit]:
        """The pgvector arm: the same filters and the same scores as the baseline,
        computed by ``<=>`` (cosine distance) in the database.

        One hit per embedding row, highest cosine first, like the baseline. A
        zero-norm vector has no cosine: pgvector yields NaN, which the baseline
        scores 0.0, so NaN is read as distance 1 (cosine 0). Ties are broken by
        id so the order is deterministic."""
        d = self.dialect()
        model_sql = ""
        if embed_models:
            model_sql = f" AND me.embed_model IN ({d.placeholder(len(embed_models))})"
        q = "[" + ",".join(map(repr, map(float, query_vector))) + "]"
        dist = f"COALESCE(NULLIF(me.embedding_vec <=> {d.param()}::vector, 'NaN'::float8), 1)"
        cur.execute(
            f"""
            SELECT mi.id AS id, 1 - {dist} AS score
            FROM memory_items mi
            JOIN memory_embeddings me ON mi.id = me.memory_id
            WHERE {self.dialect().live_predicate("mi", as_of)} AND me.dim = {d.param()}{model_sql}{tenancy_sql}
            ORDER BY score DESC, me.id
            LIMIT {d.param()}
            """,
            (q, dim, *embed_models, *tenancy_params, limit),
        )
        return [VectorHit(memory_id=r[0], score=float(r[1])) for r in cur.fetchall()]

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
        as_of: str = "",
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
            WHERE {self.dialect().live_predicate("mi", as_of)} AND me.dim = {d.param()}{model_sql}{tenancy_sql}
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
