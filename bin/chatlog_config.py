"""
chatlog_config.py — configuration resolver for the chat log subsystem.

The three-mode (integrated/separate/hybrid) system has been collapsed: there
is now only a single chatlog DB path. If it happens to equal the main memory
DB path, chat log rows live in the main store (equivalent to the old
"integrated" behavior). Otherwise they live in a dedicated file (equivalent
to "separate"), and promote operations ATTACH the main DB and copy rows
across (what used to be called "hybrid" is just copy=True, which is the
default).

Resolution order for the chatlog DB path:
    1. the configured chatlog store: a script's --chatlog-db pin
       (m3_core.paths.configured_chatlog_db); no env name is read
    2. active_database() ContextVar (per-call override set by the MCP tool
       dispatcher when a caller passes `database` on a chatlog_* tool)
    3. .chatlog_config.json db_path field, when it pins one
    4. the configured main DB (a script's --database pin, else the legacy
       M3_DATABASE env) — unified: chatlog shares it
    5. Default: memory/agent_chatlog.db (separate file; historical default)

Consumers:
    bin/chatlog_core.py       - write queue, search, promote, cost report
    bin/chatlog_status.py     - observability summary
    bin/chatlog_init.py       - interactive setup
    bin/chatlog_ingest.py     - stdin → bulk write
    bin/migrate_memory.py     - multi-target migration runner
    bin/m3_sdk.py             - get_chatlog_conn()

Zero dependency on memory_core, memory_bridge, or mcp_tool_catalog. Safe to import
from any module in bin/ without creating cycles.

Deprecated env var:
    CHATLOG_MODE — ignored with a warning. The former "integrated" behavior
    is now achieved by setting CHATLOG_DB_PATH equal to the main DB (or by
    leaving it unset when a shared --database covers everything).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlite_pragmas import apply_pragmas, refresh_schema

logger = logging.getLogger("chatlog_config")

from m3_core.paths import configured_main_db, pin_chatlog_db, pin_database  # noqa: E402
from m3_sdk import get_m3_config_root, get_m3_engine_root

_M3_CONFIG_ROOT = get_m3_config_root()
_M3_ENGINE_ROOT = get_m3_engine_root()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Path resolution (new-root-with-legacy-fallback) is the single source of truth
# in m3_core.paths; these module-local names are thin aliases kept for any
# in-module / external caller that referenced them.
from m3_core.config import settings as _m3cfg
from m3_core.paths import resolve_config_file as _resolve_config_file  # noqa: E402
from m3_core.paths import resolve_engine_file as _resolve_engine_file  # noqa: E402

CONFIG_PATH = _resolve_config_file(".chatlog_config.json")
DEFAULT_DB_PATH = _resolve_engine_file("agent_chatlog.db")
MAIN_DB_PATH = configured_main_db() or _resolve_engine_file("agent_memory.db")
STATE_FILE = _resolve_engine_file(".chatlog_state.json")
# Explicit override ONLY (tests, `chatlog_ingest --spill-dir`). None = resolve
# from the engine root at CALL time via spill_dir(): a constant bound at import
# keeps whatever root was live then, which is how test runs spilled into the
# developer's real engine dir. Read it through spill_dir(), never directly.
SPILL_DIR: str | None = None
INGEST_CURSOR = _resolve_engine_file(".chatlog_ingest_cursor.json")
# Migration DDL ships WITH the code, not in the relocatable engine data root —
# mirror the main runner (migrate_memory.MIGRATIONS_DIR = BASE_DIR/memory/migrations).
# Using _resolve_engine_file here was a bug: it pointed at <engine>/chatlog_migrations,
# which homecoming never populates, so the chatlog DB could never be bootstrapped.
CHATLOG_MIGRATIONS_DIR = os.path.join(BASE_DIR, "memory", "chatlog_migrations")

# ── Spill-dir layout: ONE owner for the writer, the drain, status and the loop ──
# <SPILL_DIR>/YYYYMMDD.jsonl                   live day file (writer appends)
# <SPILL_DIR>/YYYYMMDD.draining-<tag>.jsonl    claimed by a drain (writer never touches)
# <SPILL_DIR>/quarantine/                      undrainable files + malformed lines (.bad)
# Every *.jsonl directly under SPILL_DIR is pending work.
QUARANTINE_DIRNAME = "quarantine"
DRAINING_MARKER = ".draining-"
# Sidecar written next to a quarantined file; carries the reason, not turns.
QUARANTINE_REASON_SUFFIX = ".reason.json"


def spill_dir() -> str:
    """The spill directory: the explicit override, else <engine root>/chatlog_spill now."""
    return SPILL_DIR or _resolve_engine_file("chatlog_spill")


def spill_lock_path() -> str:
    """Lock serialising a spill APPEND with a drain's claim-by-rename, so no line
    is appended to a file after the drain has read it."""
    return os.path.join(spill_dir(), ".spill.lock")


def quarantine_dir() -> str:
    return os.path.join(spill_dir(), QUARANTINE_DIRNAME)


def spill_inventory(now: float | None = None) -> dict:
    """Pending and quarantined spill on disk, measured (never from the state file).

    pending_*: *.jsonl directly under spill_dir() (live day files and claimed
    draining files) — turns not yet in any store. quarantine_*: files under
    quarantine/ except the reason sidecars — turns preserved but parked until an
    operator moves them back. ``error`` is set (and the counts are partial) when
    a directory could not be listed.
    """
    import time

    now = time.time() if now is None else now
    sdir = spill_dir()
    inv: dict = {
        "spill_dir": sdir,
        "pending_files": 0,
        "pending_bytes": 0,
        "oldest_ms_ago": None,
        "quarantine_dir": quarantine_dir(),
        "quarantine_files": 0,
        "quarantine_bytes": 0,
        "error": None,
    }
    oldest = None
    try:
        entries = os.listdir(sdir) if os.path.isdir(sdir) else []
    except OSError as e:
        inv["error"] = f"cannot list {sdir}: {type(e).__name__}: {e}"
        entries = []
    for fn in entries:
        if not fn.endswith(".jsonl"):
            continue
        fp = os.path.join(sdir, fn)
        try:
            st = os.stat(fp)
        except OSError:
            continue  # drained/renamed between listdir and stat
        inv["pending_files"] += 1
        inv["pending_bytes"] += st.st_size
        if oldest is None or st.st_mtime < oldest:
            oldest = st.st_mtime
    if oldest is not None:
        inv["oldest_ms_ago"] = int((now - oldest) * 1000)
    qdir = quarantine_dir()
    try:
        qentries = os.listdir(qdir) if os.path.isdir(qdir) else []
    except OSError as e:
        inv["error"] = f"cannot list {qdir}: {type(e).__name__}: {e}"
        qentries = []
    for fn in qentries:
        if fn.endswith(QUARANTINE_REASON_SUFFIX):
            continue
        fp = os.path.join(qdir, fn)
        # Files only: an archive folder such as drained-YYYYMMDD/ holds turns
        # already put back, and counting it raised "1 quarantined spill file"
        # when the quarantine held no parked file (2026-10-17).
        if not os.path.isfile(fp):
            continue
        try:
            inv["quarantine_bytes"] += os.path.getsize(fp)
            inv["quarantine_files"] += 1
        except OSError:
            continue
    return inv


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def locked(lock_path: str, *, purpose: str):
    """Exclusive across threads AND processes. file_lock alone is not: its native
    advisory lock is per process, so two threads of one process (the flush
    thread and the event loop) both acquired it and lost each other's update."""
    from m3_core.locking import file_lock

    key = os.path.normcase(os.path.abspath(lock_path))
    with _THREAD_LOCKS_GUARD:
        tlock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    with tlock, file_lock(lock_path, purpose=purpose):
        yield


# ── Chatlog state file: shared by the writer, the sweeper and status ──
# Three processes read-modify-write it; unlocked, one's update erases another's.


def read_state() -> dict:
    """The state file as a dict; {} when absent. A corrupt file logs a warning
    (naming the path) and reads as {} so the next locked write repairs it."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"chatlog state file {STATE_FILE} unreadable ({type(e).__name__}: {e}); treating as empty")
        return {}


def update_state(mutate) -> dict:
    """Locked read-modify-write of the state file. ``mutate(state)`` edits the
    dict in place. Raises on lock or write failure — callers decide how loud."""
    from m3_core.atomic_io import atomic_write

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with locked(STATE_FILE + ".lock", purpose="chatlog state file"):
        state = read_state()
        mutate(state)
        atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))
    return state


def record_spill_state() -> dict:
    """Measure the spill dir and store it as state["spill"] (what the status
    line reads). Called by the writer after a spill and by the drain after a
    pass, so quarantined files show up without a status call. Raises on write
    failure."""
    from m3_core.runtime import utc_iso

    inv = spill_inventory()

    def _mutate(state: dict) -> None:
        state["spill"] = {
            "bytes": inv["pending_bytes"],
            "files": inv["pending_files"],
            "oldest_ms_ago": inv["oldest_ms_ago"],
            "quarantine_files": inv["quarantine_files"],
            "quarantine_bytes": inv["quarantine_bytes"],
            "quarantine_dir": inv["quarantine_dir"],
            "measured_at": utc_iso(),
        }

    update_state(_mutate)
    return inv


# ⚠ THE SINGLE OWNER. chatlog_core imports this rather than restating it, and
# ChatlogConfig.host_agents is built from it. Adding an agent here is not enough
# to make capture work — it also needs a hook or watcher under bin/hooks/chatlog
# and a wiring path, which tests/test_host_agent_capture_parity.py enforces.
# ("openclaw" is distinct from "opencode": different products, both supported.)
VALID_HOST_AGENTS: frozenset[str] = frozenset(
    (
        "claude-code",
        "codex",
        "gemini-cli",
        "antigravity-cli",
        "opencode",
        "openclaw",
        "aider",
        "langchain",
        "hermes",
    )
)
# Host agents that are LIBRARIES, not CLIs with a session lifecycle: the
# embedding application submits turns through m3's API, so there is no hook to
# wire and no transcript to parse. The single owner of that distinction: hook
# paths, ingest, the setup prompts and doctor all read it, instead of each
# treating such an agent as a missing hook (which handed users unknown.sh).
# Hermes Agent hands turns to m3 from its memory-provider plugin (sync_turn).
LIBRARY_HOST_AGENTS: frozenset[str] = frozenset(("langchain", "hermes"))
VALID_PROVIDERS: frozenset[str] = frozenset(
    (
        "anthropic",
        "google",
        "openai",
        "local",
        "xai",
        "deepseek",
        "mistral",
        "meta",
        "other",
    )
)


# ── Dataclasses ───────────────────────────────────────────────────────────────
@dataclass(slots=True)
class HookSpec:
    enabled: bool = False
    hook_path: str = ""
    last_seen: str = ""
    # claude-code only: if True, also capture on every assistant turn (Stop hook)
    # in addition to PreCompact. Default off — PreCompact alone is enough for
    # most users and avoids a Python spawn per turn. Cursor dedupes either way.
    stop_hook: bool = False


@dataclass(slots=True)
class EmbedSweeperSpec:
    batch_size: int = 256
    interval_min: int = 30


@dataclass(slots=True)
class RedactionSpec:
    enabled: bool = False  # OFF by default — local-first, opt-in
    patterns: list[str] = field(
        default_factory=lambda: [
            "api_keys",
            "bearer_tokens",
            "jwt",
            "aws_keys",
            "github_tokens",
        ]
    )
    custom_regex: list[str] = field(default_factory=list)
    redact_pii: bool = False
    store_original_hash: bool = True


@dataclass(slots=True)
class CostTrackingSpec:
    enabled: bool = True  # ON by default — zero user-visible cost


@dataclass(slots=True)
class ChatlogConfig:
    db_path: str = DEFAULT_DB_PATH
    # Derived from VALID_HOST_AGENTS, not restated: a hand-written map here
    # drifted from the allowlist means an agent is either registered-but-invalid
    # or valid-but-unregistered, and both fail as missing capture rather than as
    # an error (§10a — one owner).
    host_agents: dict[str, HookSpec] = field(default_factory=lambda: {a: HookSpec() for a in sorted(VALID_HOST_AGENTS)})
    embed_default: bool = False
    embed_sweeper: EmbedSweeperSpec = field(default_factory=EmbedSweeperSpec)
    queue_flush_rows: int = 200
    queue_flush_ms: int = 1500
    queue_max_depth: int = 20_000
    backpressure: Literal["spill_to_disk", "block", "drop"] = "spill_to_disk"
    cost_tracking: CostTrackingSpec = field(default_factory=CostTrackingSpec)
    redaction: RedactionSpec = field(default_factory=RedactionSpec)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ── Cache ─────────────────────────────────────────────────────────────────────
_CACHE: ChatlogConfig | None = None
_CACHE_LOCK = threading.Lock()
_MODE_WARN_EMITTED = False


def invalidate_cache() -> None:
    """Drop the cached config so the next resolve_config() re-reads env + file."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


# ── File I/O ──────────────────────────────────────────────────────────────────
def _load_file() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("Chatlog config at %s is not an object; ignoring.", CONFIG_PATH)
                return {}
            return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read %s: %s", CONFIG_PATH, e)
        return {}


def save_config(cfg: ChatlogConfig) -> None:
    """Persist cfg to CONFIG_PATH as pretty JSON (atomic rename)."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    from m3_core.atomic_io import atomic_write

    atomic_write(CONFIG_PATH, json.dumps(cfg.to_dict(), indent=2))
    invalidate_cache()


# ── Resolver ──────────────────────────────────────────────────────────────────
def _path_from_env() -> str | None:
    """The configured chatlog store: the --chatlog-db pin for this process, or None
    (m3_core.paths.configured_chatlog_db, the one reader). No env name is read."""
    from m3_core.paths import configured_chatlog_db

    return configured_chatlog_db()


def _pinned_path(file_data: dict) -> str | None:
    """The db_path written into .chatlog_config.json, if one is pinned there."""
    v = file_data.get("db_path")
    return (v.strip() or None) if isinstance(v, str) else None


def _unified_fallback(file_data: dict) -> str | None:
    """The configured main DB as the chatlog path, unless the config file pins one.

    Hazard: the --database pin (formerly the M3_DATABASE env write) names the
    MAIN store for a single process (the cognitive loop, a launchd/systemd job,
    a harness). Followed unconditionally it collapses a split install onto the
    main DB, and sync then drops the chatlog as "the same file". A pinned
    db_path is the install's own answer, so it wins; with no pin, the main DB
    still unifies (docs/CHATLOG.md).
    """
    if _pinned_path(file_data):
        return None
    return _main_path_from_env()


def pin_split_path() -> str | None:
    """Write the default (separate) chatlog path into .chatlog_config.json when
    nothing pins one, so a later --database cannot collapse this install onto
    the main store. Leaves alone a --chatlog-db or --database pin in this
    process, and a file that already pins a path.
    Returns the path written, or None."""
    if _path_from_env() is not None or _main_path_from_env() is not None:
        return None
    data = _load_file()
    if _pinned_path(data):
        return None
    data["db_path"] = DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    from m3_core.atomic_io import atomic_write

    atomic_write(CONFIG_PATH, json.dumps(data, indent=2))
    invalidate_cache()
    return DEFAULT_DB_PATH


def _main_path_from_env() -> str | None:
    """The configured main DB (the --database pin for this process, or None): on a
    unified install chatlog shares it unless a --chatlog-db pin says otherwise."""
    v = configured_main_db()
    if v is None:
        return None
    v = v.strip()
    return v or None


def _warn_if_mode_env_set() -> None:
    """CHATLOG_MODE has been removed in the unified model. Warn once per process
    so stale shell configs surface visibly without breaking the caller."""
    global _MODE_WARN_EMITTED
    if _MODE_WARN_EMITTED:
        return
    # A retired name that is set: the config snapshot is the one env reader.
    if "CHATLOG_MODE" in _m3cfg.current().retired_env:
        logger.warning(
            "CHATLOG_MODE is deprecated and ignored. The chatlog DB path is now "
            "resolved from --chatlog-db > .chatlog_config.json db_path > the main DB > "
            "default. Set db_path in .chatlog_config.json to your main DB to restore "
            "the old 'integrated' behavior."
        )
    _MODE_WARN_EMITTED = True


def _build_from_dict(d: dict) -> ChatlogConfig:
    """Construct a ChatlogConfig from a possibly-partial dict. Unknown keys
    (including the legacy `mode` field) are ignored."""
    base = ChatlogConfig()
    if not d:
        return base

    base.db_path = d.get("db_path", base.db_path) or base.db_path

    hooks_in = d.get("host_agents") or {}
    # Iterate the UNION of built-in hosts and any the config introduces, so a
    # user can register a NEW host_agent (e.g. "langchain") purely via config —
    # the validator's allowlist is built-ins ∪ config keys (see
    # chatlog_core._valid_host_agents). Previously this looped only the built-in
    # keys, silently dropping any new host a user added.
    for host in list(dict.fromkeys(list(base.host_agents.keys()) + list(hooks_in.keys()))):
        spec_in = hooks_in.get(host) or {}
        if isinstance(spec_in, dict):
            base.host_agents[host] = HookSpec(
                enabled=bool(spec_in.get("enabled", False)),
                hook_path=str(spec_in.get("hook_path", "") or ""),
                last_seen=str(spec_in.get("last_seen", "") or ""),
                stop_hook=bool(spec_in.get("stop_hook", False)),
            )

    base.embed_default = bool(d.get("embed_default", base.embed_default))

    sw = d.get("embed_sweeper") or {}
    if isinstance(sw, dict):
        base.embed_sweeper = EmbedSweeperSpec(
            batch_size=int(sw.get("batch_size", base.embed_sweeper.batch_size)),
            interval_min=int(sw.get("interval_min", base.embed_sweeper.interval_min)),
        )

    base.queue_flush_rows = int(d.get("queue_flush_rows", base.queue_flush_rows))
    base.queue_flush_ms = int(d.get("queue_flush_ms", base.queue_flush_ms))
    base.queue_max_depth = int(d.get("queue_max_depth", base.queue_max_depth))

    bp = d.get("backpressure", base.backpressure)
    if bp in ("spill_to_disk", "block", "drop"):
        base.backpressure = bp

    ct = d.get("cost_tracking") or {}
    if isinstance(ct, dict):
        base.cost_tracking = CostTrackingSpec(enabled=bool(ct.get("enabled", True)))

    rd = d.get("redaction") or {}
    if isinstance(rd, dict):
        base.redaction = RedactionSpec(
            enabled=bool(rd.get("enabled", False)),
            patterns=list(rd.get("patterns", base.redaction.patterns)),
            custom_regex=list(rd.get("custom_regex", [])),
            redact_pii=bool(rd.get("redact_pii", False)),
            store_original_hash=bool(rd.get("store_original_hash", True)),
        )

    return base


def resolve_config() -> ChatlogConfig:
    """Return the active config (redaction, host_agents, queue settings, etc.).

    Cached; call invalidate_cache() after edits. NOTE: the ``db_path`` field
    on the returned config reflects only file + CHATLOG_DB_PATH env — it does
    NOT honor the per-call active_database ContextVar. Callers that want the
    *effective* path for the current call must use ``chatlog_db_path()``,
    which re-resolves on every access.
    """
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE

        _warn_if_mode_env_set()

        file_data = _load_file()
        cfg = _build_from_dict(file_data)

        env_path = _path_from_env()
        if env_path is not None:
            cfg.db_path = env_path
        else:
            main_env = _unified_fallback(file_data)
            if main_env is not None:
                cfg.db_path = main_env

        _CACHE = cfg
        return cfg


# ── Convenience accessors ─────────────────────────────────────────────────────
def chatlog_db_path() -> str:
    """Effective chatlog DB path for the current call.

    Re-resolved on every call so the ContextVar override set by the MCP tool
    dispatcher (when a caller passes `database` on a chatlog_* tool) wins.
    Order: configured_chatlog_db() (--chatlog-db pin, legacy env) > active_database() ContextVar > pinned
    .chatlog_config.json db_path > main DB (--database pin, legacy M3_DATABASE) > default. Using the cached config's
    db_path directly would miss the ContextVar since the cache is populated
    at first-call time.
    """
    env_path = _path_from_env()
    if env_path is not None:
        return env_path
    # ContextVar — set by MCP dispatcher or CLI wrapper. Falls back to
    # the main DB + default when no override is active.
    try:
        from m3_sdk import _active_db  # lazy: avoid import cycle at module load

        ctx_val = _active_db.get()
        if ctx_val:
            return ctx_val
    except ImportError:
        pass
    main_env = _unified_fallback(_load_file())
    if main_env is not None:
        return main_env
    # Fall back to the cached config's stored db_path (file or default).
    return resolve_config().db_path


def chatlog_is_separate(path: str | None = None) -> bool:
    """THE owner of "is the chatlog (``path``, default the effective chatlog
    store) a different SQLite file from the main store, in the caller's scope?".
    Scope-relative on purpose: inside ``active_database(X)`` both resolve to X (a
    ``database=`` argument addresses chatlog tools too), so that call is unified.
    Ask it where the caller's scope is in force, e.g. at enqueue, not in the
    flush, which re-enters ``active_database(<chatlog path>)``."""
    from m3_core.paths import canonical_path, resolve_db_path

    return canonical_path(path or chatlog_db_path()) != canonical_path(resolve_db_path(None))


def chat_store_paths() -> list[str]:
    """Every store chat turns can land in, main first, de-duplicated.

    THE TRAP THIS EXISTS TO CLOSE. On a SPLIT topology (the default) turns live
    in `agent_chatlog.db` while `agent_memory.db` is a different file, so any
    caller that checks only the main DB sees ZERO chat_log rows on a perfectly
    healthy install. That has now produced the same bug three times:

      - the cognitive loop's embed gate (2026-07-25: 6,653 unembedded turns
        while the loop reported no work),
      - `m3 embedder backfill` before it reused `_embed_target_dbs`,
      - the SessionStart capture check (2026-09-07: it cried "capture NOT
        writing" while the chatlog store held 654 rows in the window).

    Each caller had hand-rolled its own answer. This is the shared one: the
    chatlog half comes from :func:`chatlog_db_path`, so it honours
    ``CHATLOG_DB_PATH``, the active-database ContextVar and a pinned
    ``db_path`` — none of which a sibling-of-the-main-DB guess would see.

    On a UNIFIED deployment both resolve to the same file and this returns one
    entry, so callers need no special case.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(p: str | None) -> None:
        if not p:
            return
        key = os.path.abspath(p)
        if key not in seen:
            seen.add(key)
            out.append(key)

    # Main store first: callers that stop at the first hit stay correct on a
    # unified deployment.
    try:
        from m3_sdk import resolve_db_path  # lazy: avoid an import cycle

        _add(resolve_db_path(None))
    except Exception:  # noqa: BLE001 — fall through to the chatlog half
        _add(_main_path_from_env())
    # Deliberately NOT chatlog_db_path() here. With no explicit
    # CHATLOG_DB_PATH it falls back to the main DB (the "one store = unified
    # DB" convenience). The cognitive loop ALWAYS pins --database, so on a
    # SPLIT install this would hand back the MAIN db, the list would collapse
    # to one entry, and the real chatlog store would never be visited — the
    # precise bug `_embed_target_dbs` was written to avoid (2026-07-25: 6,653
    # unembedded turns). Prefer the explicit override, else the configured or
    # default chatlog path from the config file.
    try:
        chat = _path_from_env()
        if not chat:
            chat = _build_from_dict(_load_file()).db_path
        _add(chat)
    except Exception:  # noqa: BLE001 — a broken chatlog config must not hide
        # the main store from a caller that only needs one path.
        pass
    return out


# ── Connection pool ───────────────────────────────────────────────────────────
_POOL: queue.Queue[sqlite3.Connection] | None = None
_POOL_LOCK = threading.Lock()
_POOL_DB_PATH: str | None = None  # path the pool was opened against; if it changes, rebuild


def _build_pool(db_path: str) -> queue.Queue[sqlite3.Connection]:
    pool_size = _m3cfg.setting("chatlog.db.pool_size")
    pool_timeout = _m3cfg.setting("chatlog.db.pool_timeout_s")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    q: queue.Queue[sqlite3.Connection] = queue.Queue(maxsize=pool_size)
    for _ in range(pool_size):
        conn = sqlite3.connect(db_path, check_same_thread=False, timeout=pool_timeout)
        conn.row_factory = sqlite3.Row
        # Centralised pragma stack — "chatlog" profile matches the values that
        # shipped in the previous inline _CHATLOG_PRAGMAS block exactly.
        apply_pragmas(conn, "chatlog")
        q.put(conn)
    return q


def _ensure_pool() -> queue.Queue[sqlite3.Connection]:
    global _POOL, _POOL_DB_PATH
    target = chatlog_db_path()
    with _POOL_LOCK:
        if _POOL is None or _POOL_DB_PATH != target:
            # Drop existing pool (connections will be GC'd when refs vanish)
            _POOL = _build_pool(target)
            _POOL_DB_PATH = target
        return _POOL


@contextmanager
def chatlog_sqlite_conn():
    """Context-managed SQLite connection from the chatlog pool.

    SQLite-only, as the name says: a connection from the chatlog-tuned pool
    (larger mmap/cache, 30s busy_timeout) opened on the chatlog DB file. When
    chatlog_db_path() happens to equal the main DB, the pool here is still
    chatlog-tuned; callers that want the main-DB pool route through
    ``M3Context.get_chatlog_conn`` (path-equality).

    Backend routing does NOT live here. ``M3Context.get_chatlog_conn`` is the
    single chokepoint that decides engine: on PostgreSQL it returns the core
    backend connection directly and never calls this function (chatlog is
    chat_log_* tables in the same PG database as core). So this stays honestly
    SQLite-only — the name is not a lie.
    """
    pool = _ensure_pool()
    timeout = _m3cfg.setting("chatlog.db.pool_timeout_s")
    conn = pool.get(timeout=timeout)
    try:
        refresh_schema(conn)
        yield conn
    finally:
        pool.put(conn)


# ── CLI self-test ─────────────────────────────────────────────────────────────
def _selftest() -> None:
    # Pin > file > default
    pin_chatlog_db(None)
    pin_database(None)
    invalidate_cache()
    c = resolve_config()
    assert c.db_path == DEFAULT_DB_PATH

    pin_database(MAIN_DB_PATH)
    invalidate_cache()
    assert chatlog_db_path() == MAIN_DB_PATH
    pin_database(None)
    import tempfile

    tmp_db = os.path.join(tempfile.gettempdir(), "alt_chatlog.db")
    pin_chatlog_db(tmp_db)
    invalidate_cache()
    assert resolve_config().db_path == tmp_db
    pin_chatlog_db(None)
    invalidate_cache()

    print("chatlog_config.py self-tests passed")


if __name__ == "__main__":
    _selftest()


# ── Capture lull ──────────────────────────────────────────────────────────────
# A 0-in-15min window is only a genuine "capture down" signal if the LAST write
# was also long ago. On a just-started session (or an ordinary between-turns
# lull), the Stop/PreCompact hook has simply not fired yet, so the 15-min count
# is legitimately 0 while the store is perfectly healthy — the last write is just
# minutes-to-hours old. Screaming "capture may be down" in that case is a false
# alarm that trips the CLAUDE.md session-start check every single fresh session
# (observed 2026-07-12: alarm fired while this very session's turns were landing).
# Grace: if the last write is within this many minutes, downgrade the scream to a
# benign note. Beyond it (or last-write unknown), the real alarm still fires.
_LULL_GRACE_MIN = 360  # 6h — comfortably longer than any between-turns gap


def capture_idle_minutes(last_write_at: str | None) -> float | None:
    """THE lull predicate: minutes since the last landed write when that is
    within _LULL_GRACE_MIN (capture idle, not down); None when the last write is
    older, missing or unparseable (a real alarm). Single owner, shared by
    chatlog_status and the SessionStart hook so the two cannot disagree."""
    mins = _minutes_since(last_write_at)
    if mins is not None and mins <= _LULL_GRACE_MIN:
        return mins
    return None


def _minutes_since(iso_ts: str | None) -> float | None:
    """Minutes between `iso_ts` (an ISO-8601 UTC stamp) and now, or None if the
    stamp is missing/unparseable. Tolerant of a trailing 'Z' and of fractional
    seconds, matching how last_write_at is stored."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None
