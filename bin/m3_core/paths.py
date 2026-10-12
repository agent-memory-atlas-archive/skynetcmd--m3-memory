import argparse
import contextvars
import enum
import logging
import os
import platform
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast, overload, override

if TYPE_CHECKING:
    from memory.backends.base import BackendName

from m3_core.seams import is_windows
from m3_core.seams import roots as _roots

logger = logging.getLogger("M3_SDK")

# ── Deprecated env-var compatibility shim ─────────────────────────────────────
# m3-specific config vars are being namespaced under M3_*. Old un-namespaced
# names (PG_URL, tuning knobs, ...) are collision-prone with
# other tools. getenv_compat reads the new name, falls back to the old one with
# a ONE-TIME deprecation warning, and records the hit so `m3 doctor` can report
# which deprecated vars are still in use (fail loud + observable, §3).
#
# DEPRECATED_ENV_RENAMES is the STATIC, authoritative old_name -> new_name map
# for the M3_* namespacing migration. It is the source of truth for the on-disk
# env-migration helper (`m3 doctor` scans config files for these OLD names and
# `--fix` rewrites them), which cannot rely on _DEPRECATED_ENV_SEEN (that only
# records names READ this process — a var sitting unread in a settings.json env
# block would never appear). Every getenv_compat(new, old) call site MUST have a
# matching entry here; tests/test_env_rename_map_drift.py enforces that so the map
# can't silently fall out of sync with the call sites. Aliases of config-registry
# keys live in the registry instead (registry_env_renames(); plan 2D.1).
DEPRECATED_ENV_RENAMES: dict[str, str] = {
    "AGENT_DB": "M3_AGENT_DB",
    "CHATLOG_DB": "M3_CHATLOG_DB",
    "CHATLOG_DB_PATH": "M3_CHATLOG_DB_PATH",
    "LM_READ_TIMEOUT": "M3_LM_READ_TIMEOUT",
    # CHROMA_BASE_URL is NOT here: the ChromaDB feature was retired, so there is
    # no live getenv_compat call site for it. It moved to RETIRED_ENV_RENAMES so
    # `m3 doctor` still cleans a stale CHROMA_BASE_URL out of on-disk config. See
    # below.
    "DB_POOL_SIZE": "M3_DB_POOL_SIZE",
    "DB_POOL_TIMEOUT": "M3_DB_POOL_TIMEOUT",
    "ORIGIN_DEVICE": "M3_ORIGIN_DEVICE",
    # PG_URL is NOT here: it was split by role (primary vs warehouse), so it lives
    # in ROLE_SPLIT_ENV_RENAMES (-> M3_CDW_PG_URL) and is resolved by the
    # role-specific resolvers, not getenv_compat. See below.
}

# ROLE_SPLIT_ENV_RENAMES: deprecations that are NOT pure M3_ namespacing because
# the var was split by role (one old name -> a role-specific new name). Kept
# separate from DEPRECATED_ENV_RENAMES so the "new == M3_ + old" invariant on that
# map (test_env_rename_map_drift.test_rename_map_is_pure_namespacing) still holds.
# `m3 doctor` scans BOTH maps when reporting/rewriting on-disk config (via
# all_env_renames()); getenv_compat only consults the pure-namespacing map.
#
# PG_URL was overloaded (primary store vs data-warehouse). The doctor-fix target
# is the WAREHOUSE name, since that is what a legacy PG_URL almost always meant
# (pg_sync). An operator who actually wanted a PG PRIMARY store sets
# M3_PRIMARY_PG_URL by hand — doctor can't tell the two intents apart, so it
# rewrites to the common case and the deprecation message names both.
ROLE_SPLIT_ENV_RENAMES: dict[str, str] = {
    "PG_URL": "M3_CDW_PG_URL",
}

# RETIRED_ENV_RENAMES: vars for FEATURES THAT NO LONGER EXIST. There is no live
# getenv_compat call site (the reading code is gone), so these are deliberately
# NOT in DEPRECATED_ENV_RENAMES — the drift test asserts that map has a call site
# for every entry. But `m3 doctor` should still scrub a stale old name out of a
# user's on-disk config (a settings.json env block, a dotenv line), so they are
# reported/rewritten via all_env_renames(). The rename is cosmetic: nothing reads
# either name anymore. Kept separate from ROLE_SPLIT so the "new == M3_ + old"
# invariant on THAT map is not muddied.
#
#   * CHROMA_BASE_URL — ChromaDB (optional L3 sync backend) was retired. Nothing
#     reads it; doctor renames M3_-consistently so the old name stops lingering.
RETIRED_ENV_RENAMES: dict[str, str] = {
    "CHROMA_BASE_URL": "M3_CHROMA_BASE_URL",
}


def all_env_renames() -> dict[str, str]:
    """Union of the pure-namespacing, role-split, and retired deprecation maps.

    The single source of truth for `m3 doctor`'s on-disk config scan/rewrite:
    every old env-var KEY that should be reported and renamed, mapped to its new
    name. getenv_compat does NOT use this (it only does pure namespacing); the
    role-split names have dedicated resolvers (resolve_cdw_pg_dsn, etc.), and the
    retired names have no reader at all — doctor just scrubs stale config.
    """
    merged = dict(DEPRECATED_ENV_RENAMES)
    merged.update(ROLE_SPLIT_ENV_RENAMES)
    merged.update(RETIRED_ENV_RENAMES)
    merged.update(registry_env_renames())
    return merged


def registry_env_renames() -> dict[str, str]:
    """{legacy alias: canonical name} for every config-registry key. Keys read
    through the registry (plan 2D.1 onward) resolve their aliases there, not via
    getenv_compat, so their pairs are not in DEPRECATED_ENV_RENAMES (whose drift
    test pins it to getenv_compat call sites); the registry is their one owner."""
    from m3_core.config import REGISTRY

    return {alias: key.env_names[0] for key in REGISTRY for alias in key.env_names[1:]}


# _DEPRECATED_ENV_SEEN maps old_name -> new_name for every deprecated var that
# was actually read from the environment this process. Doctor reads it via
# deprecated_env_in_use().
_DEPRECATED_ENV_SEEN: dict[str, str] = {}
_DEPRECATED_ENV_WARNED: set[str] = set()


@overload
def getenv_compat(new_name: str, old_name: str, default: str) -> str: ...
@overload
def getenv_compat(new_name: str, old_name: str, default: None = None) -> str | None: ...
def getenv_compat(new_name: str, old_name: str, default: str | None = None) -> str | None:
    """Resolve an env var during the M3_* namespacing migration.

    Precedence: new_name > old_name > default. If only the old (deprecated)
    name is set, warn once and record it in _DEPRECATED_ENV_SEEN so `m3 doctor`
    can surface it. The new name always wins, so users can migrate incrementally.
    """
    val = os.environ.get(new_name)
    if val is not None:
        return val
    old_val = os.environ.get(old_name)
    if old_val is not None:
        _DEPRECATED_ENV_SEEN[old_name] = new_name
        if old_name not in _DEPRECATED_ENV_WARNED:
            _DEPRECATED_ENV_WARNED.add(old_name)
            logger.warning(
                "Deprecated env var %s is set; use %s instead. The old name still "
                "works for now but will be removed. (`m3 doctor` lists all in use.)",
                old_name,
                new_name,
            )
        return old_val
    return default


def deprecated_env_in_use() -> dict[str, str]:
    """Return {old_name: new_name} for every deprecated env var read so far this
    process (via getenv_compat OR the role-specific PG resolvers). Used by
    `m3 doctor` to report migration TODOs. Only reflects names actually READ — a
    var set but never consulted won't show.

    Keys read through the config registry (plan 2D.1 onward) resolve their legacy
    aliases there, not through getenv_compat, so their half comes from the
    config snapshot. A config file that does not load is reported by doctor's
    own config check; here it only means the registry half is unknown, and the
    log says so rather than the report quietly shrinking (§3).
    """
    seen = dict(_DEPRECATED_ENV_SEEN)
    try:
        from m3_core.config import settings as _settings

        seen.update(_settings.current().deprecated_aliases_in_use())
    except Exception as e:  # noqa: BLE001 -- see docstring
        logger.warning(
            "deprecated env report: the config registry's legacy aliases are not "
            "included (observed: %s: %s). inspect: `m3 config show`",
            type(e).__name__,
            e,
        )
    return seen


# ── PostgreSQL DSN resolution: two ROLES, deliberately separated ──────────────
# m3 uses PostgreSQL in two unrelated roles that historically read the SAME env
# var (PG_URL / M3_PG_URL) — a footgun, because setting the warehouse DSN would
# silently arm the primary-store path (and vice-versa). They are separated:
#
#   * PRIMARY store: a single instance's authoritative read/write DB, stored as
#     the secret M3_PRIMARY_PG_URL. NEVER resolves a warehouse DSN.
#   * CDW / WAREHOUSE (pg_sync fan-in mirror, `m3_warehouse` namespace): a shared
#     aggregate many instances UPSERT into, stored as M3_CDW_PG_URL (an older
#     store may hold it as PG_URL).
#
# Both resolve through the secrets chain (m3_core.secret_store: the secrets file,
# the keyring, the vault) and NEVER read the environment (m3 9cbed0af). These
# two functions are the SDK's names for it.

# The canonical rename: old PG_URL -> the warehouse (CDW) name. This is NOT pure
# M3_ namespacing (PG_URL -> M3_CDW_PG_URL, not M3_PG_URL), so it is tracked here
# rather than in DEPRECATED_ENV_RENAMES (which is namespacing-only).
PG_URL_DEPRECATION: tuple[str, str] = ("PG_URL", "M3_CDW_PG_URL")


def resolve_primary_pg_dsn(default: str | None = None) -> str | None:
    """DSN for the PRIMARY PostgreSQL store, from the secrets chain
    (``secret_store.resolve_pg_dsn``), else ``default``. Never a warehouse DSN,
    never the environment."""
    from m3_core.secret_store import PgRole, resolve_pg_dsn

    return resolve_pg_dsn(PgRole.PRIMARY) or default


def resolve_cdw_pg_dsn(default: str | None = None) -> str | None:
    """DSN for the CDW / data-warehouse PostgreSQL (pg_sync fan-in mirror), from
    the secrets chain, else ``default``. Never the primary DSN, never the
    environment."""
    from m3_core.secret_store import PgRole, resolve_pg_dsn

    return resolve_pg_dsn(PgRole.WAREHOUSE) or default


def assert_no_deprecated_pg_url_on_install() -> None:
    """For SDK callers: raise if the old PG_URL env var is set.

    m3 itself no longer reads a DSN from the environment, so a PG_URL there is
    a plaintext warehouse DSN m3 ignores, and an SDK caller that still exports
    it is relying on behaviour that is gone. Raises RuntimeError with the fix.
    """
    old, new = PG_URL_DEPRECATION
    if os.environ.get(old) is not None:
        raise RuntimeError(
            f"observed: the old env var {old} is set; m3 does not read DSNs from the environment. "
            f"fix: store the warehouse DSN with `m3 secret set {new}` (or a PostgreSQL primary "
            f"store with `m3 secret set M3_PRIMARY_PG_URL`), or move it with `m3 config import-env`, "
            f"then unset {old}."
        )


# ── Active-database ContextVar ────────────────────────────────────────────────
# Consulted by callers that want "whatever DB the surrounding request/CLI
# specified, else the default". The MCP tool dispatcher sets this before each
# tool call; CLI scripts set it once at startup.
_active_db: contextvars.ContextVar[str | None] = contextvars.ContextVar("m3_active_db", default=None)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreRef:
    """A named store as the resolvers need it (CONFIG_DESIGN §14). ``path`` is the
    SQLite file (absolute) or None; ``dsn_secret`` names a PostgreSQL store's
    bootstrap secret. Built and validated by ``m3_core.config.stores``."""

    name: str
    backend: str
    path: str | None = None
    dsn_secret: str | None = None


class _Unset(enum.Enum):
    UNSET = enum.auto()


# The ACTIVE named store, set only by m3_core.config.stores.active_store(): a
# StoreRef, None for an explicit "main", or UNSET (defer to the process's store).
# Read it only through current_store(). A store outranks the legacy SQLite path
# (M3_DATABASE / active_database) in resolve_db_path and decides
# resolve_backend_name: §14's order, user decision D3 (2026-10-09). With no store
# anywhere, both resolvers behave exactly as before 2C.3.
# A store, None for an explicit "main", or UNSET (defer to the process's store).
type _StoreSelection = StoreRef | None | Literal[_Unset.UNSET]

_active_store: contextvars.ContextVar[_StoreSelection] = contextvars.ContextVar("m3_active_store", default=_Unset.UNSET)

# The PROCESS's store, set once by a CLI's --store (m3_core.config.stores
# .set_process_store). Below the ContextVar, so a per-call store= or "main"
# overrides it. A module global, not a ContextVar set in main(): a new thread
# starts with an empty context, so a ThreadPoolExecutor worker would see no store
# and use the default one without a word.
_process_store: StoreRef | None = None


def current_store() -> StoreRef | None:
    """The store in effect: this context's selection, else the process's, else
    None (the default store, ``main``). The one reader of both."""
    store = _active_store.get()
    return _process_store if store is _Unset.UNSET else store


@contextmanager
def without_active_store():
    """Run a block as if no named store were active. The chatlog is PROCESS-WIDE
    (user decision D1, 2026-10-09): a `store=` call must not move it, so the
    chatlog's connection is resolved inside this."""
    token = _active_store.set(None)
    try:
        yield
    finally:
        _active_store.reset(token)


def resolve_venv_python() -> str:
    """Returns the path to the project venv Python executable, cross-platform."""
    # __file__ is bin/m3_core/paths.py; three dirnames reach the repo root, the
    # same base the original bin/m3_sdk.py computed with two dirnames.
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if is_windows():
        return os.path.join(base, ".venv", "Scripts", "python.exe")
    return os.path.join(base, ".venv", "bin", "python")


def reexec_in_project_venv() -> None:
    """Re-run this script under the project's ``.venv`` when it was started by an
    interpreter OUTSIDE any venv (a dev checkout run with the system Python).

    Never from inside a venv: a pip/pipx install already runs in its own, and a
    stray ``.venv`` beside an installed payload (found 2026-10-11 under
    ``site-packages/m3_memory``) re-ran the hourly sync under an interpreter nobody
    chose, through a Windows "exec" that flashed a window and reported exit 0 for
    every run (``m3_memory._platform.reexec``). The one owner: pg_sync and the two
    knowledge CLIs each carried a copy."""
    if sys.prefix != sys.base_prefix:
        return
    venv_python = resolve_venv_python()
    if not os.path.exists(venv_python) or os.path.abspath(sys.executable) == os.path.abspath(venv_python):
        return
    from m3_core.seams import reexec

    reexec([venv_python, *sys.argv])


def canonical_path(p: "str | os.PathLike[str]") -> str:
    """The lexical absolute form of ``p``, as a string: the one owner of path IDENTITY.

    Use it wherever a path string is stored, compared, de-duplicated or matched
    as a prefix. It is ``os.path.abspath``: absolute, ``..`` collapsed, host
    separators, symlinks NOT followed. pathlib has no equivalent:
    ``Path.absolute()`` keeps ``..`` (so ``a/../b`` and ``b`` would differ), and
    ``Path.resolve()`` follows symlinks and junctions and can turn a mapped drive
    into a UNC path. On a ``canonical_path`` result, pathlib's ``/``, ``.parent`` and
    ``.name`` match ``os.path.join``/``dirname``/``basename`` byte for byte
    (measured 2026-10-10 on 3.12 and 3.14, Windows and Linux), so build on it.
    """
    return os.path.abspath(p)


def expand_user(p: "str | os.PathLike[str]") -> str:
    """``~`` expanded, or ``p`` unchanged when no home can be determined.

    That is ``os.path.expanduser``. ``Path.expanduser()`` raises RuntimeError
    instead, and every root here is resolved under services, SYSTEM accounts and
    containers without HOME, where raising would stop m3 from finding anything.
    """
    return os.path.expanduser(p)


def _abs_root(root: str) -> str:
    """Absolute root, convention-aware: ``m3_memory._roots.abs_root`` (the owner)."""
    return _roots.abs_root(root)


def get_m3_root() -> str:
    """The memory root: ``M3_MEMORY_ROOT``, else ``~/.m3`` once migrated, else
    ``~/.m3-memory`` (``m3_memory._roots``)."""
    return _roots.memory_root()


def get_m3_legacy_root() -> str:
    """Where pre-decoupling state lives, for legacy FALLBACK lookups only
    (``m3_memory._roots.legacy_memory_root``): never ``get_m3_root()``, which moves
    to ``~/.m3`` at the Root-1 flip."""
    return _roots.legacy_memory_root()


def get_m3_backups_root() -> str:
    """Where database snapshots go: ``<engine root>/backups``.

    Backups are database state, so they follow the engine root (and with it
    ``M3_ENGINE_ROOT`` / ``M3_MEMORY_ROOT``) rather than the legacy
    ``get_m3_root()`` (~/.m3-memory), which the decoupled-roots layout no
    longer uses for databases. Tools add their own subdirectory.
    """
    return os.path.join(get_m3_engine_root(), "backups")


def get_m3_config_root() -> str:
    """Returns the M3 configuration directory.
    Precedence: M3_CONFIG_ROOT > M3_MEMORY_ROOT/config > ~/.m3/config
    """
    return _roots.config_root()


# Which tier of the config-root precedence answered (CONFIG_DESIGN §9).
CONFIG_ROOT_ENV = _roots.CONFIG_ROOT_ENV
CONFIG_ROOT_LEGACY = _roots.CONFIG_ROOT_LEGACY
CONFIG_ROOT_DEFAULT = _roots.CONFIG_ROOT_DEFAULT
CONFIG_ROOT_ARG = _roots.CONFIG_ROOT_ARG
CONFIG_FILE_NAME = _roots.CONFIG_FILE_NAME


def config_root_with_tier() -> tuple[str, str]:
    """(config root, tier); the precedence is owned by ``m3_memory._roots``."""
    return _roots.config_root_with_tier()


def pinned_config_file() -> str | None:
    """The ``m3 --config`` file pinned for this process, or None."""
    return _roots.pinned_config_file()


def dotenv_path() -> str | None:
    """The legacy .env m3 reads: ``<config root>/.env``, else the legacy root's ``.env``;
    None when neither exists. The one owner of that rule: M3Context injects it
    (until 2D) and the config registry parses it as its lowest source."""
    for root in (get_m3_config_root(), get_m3_legacy_root()):
        candidate = os.path.join(root, ".env")
        if os.path.exists(candidate):
            return candidate
    return None


def get_m3_engine_root() -> str:
    """Returns the M3 database engine directory.
    Precedence: M3_ENGINE_ROOT > M3_MEMORY_ROOT/engine > ~/.m3/engine
    """
    return _roots.engine_root()


def get_m3_models_root() -> str:
    """Returns the M3 model directory (GGUF weights m3 owns itself).
    Precedence: M3_MODELS_ROOT > M3_MEMORY_ROOT/models > ~/.m3/models

    This is the one model location that does not belong to a third party, so
    it is probed FIRST: a user who drops LM Studio, Ollama, or llama.cpp keeps
    a working embedder.
    """
    return _roots.models_root()


def get_m3_logs_root() -> str:
    """Returns the M3 log directory for scheduled tasks and services.
    Precedence: M3_LOGS_ROOT > M3_MEMORY_ROOT/logs > ~/.m3/logs

    Logs are user state, not payload. Deriving them from the install root put
    them inside the pipx venv (site-packages/m3_memory/logs), where nobody
    looks and a reinstall deletes them.
    """
    return _roots.logs_root()


def resolve_engine_file(filename: str) -> str:
    """Resolve a path under the engine root, honoring the legacy fallback.

    Returns ``<engine_root>/<filename>`` unless a legacy copy exists at
    ``<memory_root>/memory/<filename>`` and the new path does not — in which
    case the legacy path is returned so pre-Homecoming installs keep working.

    Single source of truth: chatlog_config, memory.config and migrate_memory
    previously each carried a byte-identical copy of this helper. They now
    import it from here so the resolution rule lives in exactly one place.
    """
    new_path = os.path.join(get_m3_engine_root(), filename)
    legacy_path = os.path.join(get_m3_legacy_root(), "memory", filename)
    if os.path.exists(legacy_path) and not os.path.exists(new_path):
        return legacy_path
    return new_path


def node_id() -> str:
    """This machine's stable identity. The single owner of that question.

    Six call sites answered this independently (auth_utils, chatlog_ingest,
    files_memory.ingest, memory.config, test_memory_bridge)
    and only ONE of them normalised case. §10a -- a copied resolution is the
    defect independent of correctness, and here the copies genuinely disagree.

    ⚠ WHY CASE MATTERS, measured and documented at ``chatlog_ingest``:
    Windows hostnames are case-insensitive and ``COMPUTERNAME`` is the canonical
    UPPERCASE form, but a shell that does not inherit it (git-bash, WSL) falls
    through to ``platform.node()``, which returns mixed case -- "HostPc" rather
    than "HOSTPC". Un-normalised, ONE machine reports TWO identities depending
    on which shell launched the agent.

    That is not cosmetic for node detection: a single-node fleet whose agents
    started from different shells would look like a multi-node fleet, and the
    dispatch backend decision would follow the wrong branch.

    POSIX hostnames ARE case-significant, so normalisation is Windows-only.

    ``M3_NODE_ID`` overrides everything, for a deployment that needs an explicit
    identity (containers sharing a hostname, or a fleet with a naming scheme).
    """
    explicit = os.environ.get("M3_NODE_ID")
    if explicit:
        return explicit.strip()
    host = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or platform.node() or "unknown"
    return host.upper() if is_windows() else host


def dispatch_store_mode() -> str:
    """``"separate"`` (default) or ``"integrated"`` for the dispatch store.

    Mirrors the chatlog's storage modes, minus ``hybrid``: that exists there
    because ``chatlog_promote`` copies rows into canonical memory, and a
    delivery record has no equivalent promotion.

    Set ``M3_DISPATCH_MODE=integrated`` to keep dispatch tables in the main
    store -- reasonable for a single-agent install where a second file is
    overhead. Anything else, including unset, means separate.
    """
    mode = (os.environ.get("M3_DISPATCH_MODE") or "").strip().lower()
    return "integrated" if mode == "integrated" else "separate"


def resolve_dispatch_db() -> str:
    """Path to the agent DISPATCH store. The single owner of that question.

    m3 keeps three kinds of state with three lifetimes, and each gets its own
    store so it can be handled on its own terms:

        agent_memory.db    permanent   durability, FTS, embeddings, GDPR
                                       erasure, backup, warehouse sync
        agent_chatlog.db   decaying    bulk ingest, decay, promotion
        agent_dispatch.db  EPHEMERAL   write churn, lease reclaim, pruning

    A delivery record is scaffolding: once the recipient acts, the row's only
    remaining value is a short audit tail, and the thing worth keeping is the
    payload in ``memory_items`` that it points at. Measured -- the rest of m3
    already treats these rows this way: ``pg_sync`` replicates memory_items,
    memory_embeddings and chat_log but has never replicated notifications, and
    ``gdpr_forget`` does not touch them. The split makes that structural.

    Secondary but real: SQLite takes one write lock for the WHOLE database, and
    dispatch is the highest-frequency writer in the system. In a shared file
    every claim/renew/complete/sweep contends with ingest and enrichment; in its
    own store it contends only with itself.

    Resolution order, matching ``chatlog_config.DEFAULT_DB_PATH``:
      1. ``M3_DISPATCH_DB`` -- explicit override.
      2. integrated mode -- the main store, so there is ONE file.
      3. ``<engine_root>/agent_dispatch.db`` via :func:`resolve_engine_file`,
         which carries the pre-Homecoming legacy fallback.

    ⚠ This returns the DATA path. Migration DDL ships with the CODE and is
    resolved separately (see :func:`dispatch_migrations_dir`); conflating the
    two is a real bug the chatlog already hit, which left its store
    unbootstrappable because ``<engine>/chatlog_migrations`` is never populated.
    """
    explicit = os.environ.get("M3_DISPATCH_DB")
    if explicit:
        return explicit
    if dispatch_store_mode() == "integrated":
        return os.environ.get("M3_DATABASE") or resolve_engine_file("agent_memory.db")
    return resolve_engine_file("agent_dispatch.db")


def dispatch_migrations_dir() -> str:
    """Directory holding the dispatch store's migration DDL.

    Ships WITH THE CODE, never under a relocatable data root. Two wrong
    resolutions are possible here and BOTH have been made in this codebase:

    * ``resolve_engine_file`` points at ``<engine>/dispatch_migrations``, which
      nothing populates -- the bug ``chatlog_config`` documents at its
      ``CHATLOG_MIGRATIONS_DIR``.
    * ``get_m3_root()`` points at the relocatable ``M3_MEMORY_ROOT``
      (``~/.m3-memory`` by default), which is the DATA root, not the code. Same
      class of error one level out; caught here by a test asserting the
      directory actually exists.

    Derived from ``__file__`` instead, matching ``chatlog_config.BASE_DIR``: the
    DDL sits beside the module that runs it, wherever the payload is installed.
    """
    # paths.py lives at <repo>/bin/m3_core/paths.py -> up three to <repo>.
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "memory", "dispatch_migrations")


def dispatch_pg_schema() -> str:
    """PostgreSQL schema holding the dispatch tables. Default ``m3_dispatch``.

    On SQLite the dispatch store is a separate FILE, because the file is
    SQLite's unit of write contention and of backup. PostgreSQL has neither
    problem, and a schema is already its unit of permissions, ``search_path``
    and selective dump -- so isolation there is a SCHEMA in the same database,
    not a second database with a second URL and a second pool.

    Several m3 fleets can share one server by giving each its own schema name.
    """
    return (os.environ.get("M3_DISPATCH_PG_SCHEMA") or "m3_dispatch").strip()


def notification_store_paths() -> "list[str]":
    """Every store an agent notification can land in, main first, de-duplicated.

    THE TRAP THIS EXISTS TO CLOSE -- and it has already been sprung three times
    for CHAT turns, which is why `chatlog_config.chat_store_paths` exists. This
    is the same shape one store later.

    On a SPLIT topology (the default) delivery records live in
    `agent_dispatch.db` while `agent_memory.db` is a different FILE. A caller
    that checks only the main store sees zero pending rows on a perfectly
    healthy install, and the failure is SILENT: no error, no log, the receipt
    SLA simply under-reports. For the waiter that is worse than an outage,
    because an agent that trusts an empty inbox drops work addressed to it.

    On an INTEGRATED deployment both resolve to the same file and this returns
    ONE entry, so callers need no special case -- iterate and stop worrying
    about the topology.

    ⚠ Order matters: the MAIN store comes first, so a caller that stops at the
    first hit stays correct on an integrated install.

    ⚠ This is the DATA question ("which files hold notifications"), not the
    schema question. A store that exists but has not been bootstrapped yet is
    still returned; callers that query it must tolerate a missing table rather
    than assume presence implies schema.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(path: "str | None") -> None:
        if not path:
            return
        key = os.path.abspath(path)
        if key not in seen:
            seen.add(key)
            out.append(key)

    # Main store first. Prefer the SDK resolver, which honours the active
    # database ContextVar and a pinned db_path -- none of which a
    # sibling-of-the-engine-root guess would see.
    try:
        from m3_sdk import resolve_db_path  # lazy: avoid an import cycle

        _add(resolve_db_path(None))
    except Exception:  # noqa: BLE001 -- fall through to the documented default
        _add(os.environ.get("M3_DATABASE") or resolve_engine_file("agent_memory.db"))

    # Dispatch store second. In integrated mode this resolves to the main store
    # and de-duplicates away, which is exactly the "no special case" property.
    try:
        _add(resolve_dispatch_db())
    except Exception:  # noqa: BLE001 -- a broken dispatch config must not hide
        # the main store from a caller that only needs one path.
        pass
    return out


def resolve_config_file(filename: str) -> str:
    """Resolve a path under the config root, honoring the legacy fallback.

    Returns ``<config_root>/<filename>`` unless a legacy copy exists at
    ``<memory_root>/memory/<filename>`` and the new path does not. Companion to
    :func:`resolve_engine_file`; see that docstring for the de-duplication note.
    """
    new_path = os.path.join(get_m3_config_root(), filename)
    legacy_path = os.path.join(get_m3_legacy_root(), "memory", filename)
    if os.path.exists(legacy_path) and not os.path.exists(new_path):
        return legacy_path
    return new_path


def _db_is_populated(path: str) -> bool:
    """True iff `path` is a SQLite file that actually carries the memory schema.

    A bare-existence check is not enough: a connection attempt against a not-yet-
    migrated engine root auto-creates a 0-table `agent_memory.db` stub, and that
    stub would otherwise shadow a populated legacy DB (the M3_MEMORY_ROOT drift —
    a fresh engine/ stub silently winning over memory/agent_memory.db with the
    real data). Returns False for a missing file, an empty stub, or any open/read
    error (treat unreadable as "not usable" so the caller keeps searching).
    """
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(path, timeout=2)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_items' LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — unreadable/locked DB is not a usable default
        return False


def _default_db_path() -> str:
    # Precedence: explicit M3_ENGINE_ROOT (honored as-is) > a *populated* derived
    # engine root > a populated ~/.m3/engine default > populated sibling memory/
    # (dev clone) > the derived engine path as a last resort (fresh install).
    #
    # The key fix over the naive "any env var set -> engine path" rule: when only
    # M3_MEMORY_ROOT is set, the engine path is DERIVED, not chosen. If that
    # derived DB is missing or an empty stub, we must not let it shadow a
    # populated legacy memory/ DB — see _db_is_populated.
    if _roots.pinned(_roots.ENGINE_ROOT_ENV):
        # Explicit engine root is a deliberate operator choice; honor it verbatim
        # even if empty (a fresh deployment legitimately starts empty here).
        return os.path.join(get_m3_engine_root(), "agent_memory.db")

    engine_db = os.path.join(get_m3_engine_root(), "agent_memory.db")
    mem_root = _roots.pinned(_roots.MEMORY_ROOT_ENV)
    if mem_root:
        if _db_is_populated(engine_db):
            return engine_db
        # Derived engine DB is missing/empty. Prefer a populated legacy memory/
        # DB under the same root before falling back to the empty engine path.
        legacy_under_root = os.path.join(
            os.path.abspath(mem_root),  # host form, as this path has always been
            "memory",
            "agent_memory.db",
        )
        if _db_is_populated(legacy_under_root):
            logger.warning(
                "M3_MEMORY_ROOT engine DB (%s) is missing or unmigrated; using the "
                "populated legacy store at %s. Run bin/homecoming.py to migrate, or "
                "set M3_ENGINE_ROOT explicitly to silence this.",
                engine_db,
                legacy_under_root,
            )
            return legacy_under_root
        return engine_db

    # No env override: prefer a populated ~/.m3/engine default, else a populated
    # sibling memory/ (developer clone), else the engine default for a fresh start.
    m3_engine_default = os.path.join(os.path.expanduser("~"), ".m3", "engine", "agent_memory.db")
    if _db_is_populated(m3_engine_default):
        return m3_engine_default

    # __file__ is bin/m3_core/paths.py; three dirnames reach the repo root, the
    # same base the original bin/m3_sdk.py computed with two dirnames.
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    legacy_path = os.path.join(base, "memory", "agent_memory.db")
    if _db_is_populated(legacy_path):
        return legacy_path

    return os.path.join(get_m3_engine_root(), "agent_memory.db")


#: URL schemes that identify a DSN rather than a filesystem path. A DSN reaching
#: this function is always a configuration error (see :func:`resolve_db_path`),
#: never something to coerce. Kept broad on purpose: any scheme here is a
#: connection string for SOME backend, and none of them is ever a SQLite file.
_DSN_SCHEMES: tuple[str, ...] = (
    "postgres://",
    "postgresql://",
    "mysql://",
    "mariadb://",
    "sqlite://",
)


def _looks_like_dsn(value: str) -> bool:
    """True when ``value`` is a connection string, not a filesystem path.

    Scheme-prefix match rather than ``urlparse``: a Windows path (``C:\\...``)
    parses as scheme ``c``, so a generic "has a scheme" test would reject the
    most common valid input on this platform.
    """
    return value.strip().lower().startswith(_DSN_SCHEMES)


def resolve_db_path(explicit: str | None = None) -> str:
    """Resolve an absolute SQLite DB path.

    Order: explicit arg > the active named SQLite store (2C.3, §14) >
    active_database ContextVar > M3_DATABASE env (legacy) > default
    (memory/agent_memory.db). Returns an absolute path so pool-cache keys are
    consistent regardless of caller CWD.

    The ContextVar outranks env (M3_DATABASE design, approved 2026-10-12, Q3
    phase 1): a call scoped with ``active_database()`` must resolve its scoped
    DB, and an env value that happened to agree used to mask a scope that was
    never set (R5).

    Raises ``ValueError`` when the resolved candidate is a DSN. This function
    answers "which SQLite FILE", so a connection string is a configuration
    error, not an input to coerce — but ``os.path.abspath`` coerces it happily,
    joining it onto the CWD (§3 fail loud, never silent)::

        M3_DATABASE=postgresql://u:p@host:5432/agent_memory
        -> C:\\Users\\bhaba\\.m3-dev\\m3-memory\\postgresql:\\u:p@host:5432\\agent_memory

    Nothing rejects that path; SQLite simply creates an empty database at it.
    The caller then reads and writes a real store that is silently EMPTY, while
    the configured PostgreSQL store goes untouched. Measured 2026-09-12: this
    wedged AgentOS_NotificationWaiter into a restart loop
    (``WinError 123``), which was the mild outcome only because the mangled
    name happened to be syntactically illegal on Windows. The same value on
    Linux is a perfectly legal path, so it would have silently created the
    stub and reported healthy — the far worse failure this guard prevents.
    """
    store = current_store()
    store_path = store.path if store is not None and store.backend == "sqlite" else None
    scoped = _active_db.get()
    legacy = os.environ.get("M3_DATABASE")
    candidate = explicit or store_path or scoped or legacy or _default_db_path()
    if _looks_like_dsn(candidate):
        # Name the source so the operator knows WHICH knob to fix; the value is
        # echoed scheme-only because a DSN carries a password.
        scheme = candidate.strip().split("://", 1)[0]
        source = "the explicit argument" if explicit else "the active_database() context" if scoped else "M3_DATABASE"
        raise ValueError(
            f"resolve_db_path() resolves a SQLite FILE path, but got a DSN. "
            f"observed: {scheme}:// DSN from {source}. "
            f"cause: a DSN here is joined onto the working directory by "
            f"abspath(), producing a path SQLite will happily create as an "
            f"empty database while the configured store goes untouched. "
            f"inspect: db.backend selects a non-SQLite backend (its DSN is a "
            f"stored secret, e.g. M3_PRIMARY_PG_URL); "
            f"M3_DATABASE must be unset or point at a SQLite file."
        )
    return os.path.abspath(candidate)


@contextmanager
def active_database(path: str | None):
    """Set the active DB path for the duration of a block (ContextVar-scoped).

    Propagates across ``await`` within the same task but does not leak across
    threads — each executor thread gets its own copy unless the caller sets it
    explicitly. Pass ``None`` or "" to defer to env/default resolution.
    """
    resolved = resolve_db_path(path) if path else None
    token = _active_db.set(resolved)
    try:
        yield resolved
    finally:
        _active_db.reset(token)


@contextmanager
def scoped_db_env(path: "str | os.PathLike[str] | None"):
    """Set ``M3_DATABASE`` for a block and restore it exactly on exit.

    PREFER :func:`active_database`. This exists for the one case it cannot
    cover: a module that must influence what a LATER ``import`` binds at import
    time (``memory.config.DB_PATH`` reads ``M3_DATABASE`` when first imported),
    which a ContextVar cannot reach. Everything else -- routing reads and writes
    at a store -- belongs in ``active_database``, which needs no env var and
    cannot leak. Use both together when both are needed: this one for the
    import, that one for the routing.

    WHY THIS IS A SEAM PRIMITIVE RATHER THAN FOUR COPIES. An unrestored write
    poisons every later ``resolve_db_path(None)`` in the process, and the
    callers that need it are loops over MULTIPLE stores (core, then chatlog),
    so the value left behind is the LAST store swept, not the main one. That
    turned into a silent wrong-store read four separate times in this codebase
    (#180). The save/restore is three lines and every copy got to decide
    independently whether to bother -- exactly the duplication §10a calls the
    defect, independent of correctness.

    RESTORES ABSENCE AS ABSENCE. ``resolve_db_path`` reads
    ``os.environ.get("M3_DATABASE") or ...``, so an empty string would fall
    through there -- but other readers test for the KEY, so putting "" back
    where nothing existed is not a restore.

    SQLITE-SHAPED BY CONTRACT, and that is not a portability gap.
    ``M3_DATABASE`` names a SQLite FILE: ``resolve_db_path`` raises on a DSN,
    and a non-SQLite backend is selected by ``db.backend`` with its own stored
    DSN secret (e.g. ``M3_PRIMARY_PG_URL``), which this function does not touch.
    So on PostgreSQL there is nothing here to scope -- callers pass the path
    they were going to set anyway, and a DSN raises from ``resolve_db_path``
    at use, as it already would.

    Accepts ``os.PathLike`` as well as ``str`` because the callers hold
    ``Path`` objects (they iterate resolved store paths) and the body stringifies
    anyway. Annotating this ``str``-only forced a ``str(...)`` at every call site
    to satisfy the type checker while changing nothing at runtime.
    """
    prev = os.environ.get("M3_DATABASE")
    if path is not None:
        os.environ["M3_DATABASE"] = str(path)
    try:
        yield prev
    finally:
        if prev is None:
            os.environ.pop("M3_DATABASE", None)
        else:
            os.environ["M3_DATABASE"] = prev


def seam_backend():
    """The active storage backend, or a SQLite-only shim during bootstrap.

    ONE definition for the ~40 scripts that must open a database without
    assuming SQLite (DESIGN_PHILOSOPHIES §10a: duplicated logic is the defect,
    independent of correctness — three copies of this helper in three backfill
    scripts is three things to drift). Use it as:

        with seam_backend().open_readonly(db_path) as conn:   # reads
        with seam_backend().connection() as conn:             # writes

    ``open_readonly`` is backend-blind by contract: SQLite honors ``db_path``
    and opens ``file:...?mode=ro``; PostgreSQL ignores it (there is one pooled
    store) and yields a normal connection. So a caller stays out of
    ``if backend ==`` ladders entirely.

    The fallback exists because several of these scripts run during installer
    bootstrap, before the seam is importable. Only ImportError reaches it: a
    PostgreSQL install has the seam on sys.path long before any of this runs,
    so a SQLite-shaped shim is the correct degradation, not a second
    implementation of the seam. Any other error is the seam refusing a bad
    configuration and propagates (§3). This caught ``Exception`` until
    2026-10-10, so PostgreSQL selected with no DSN, which the backend refuses
    with "Will not silently fall back to SQLite", got the SQLite shim instead.
    """
    try:
        from memory.backends import active_backend
    except ImportError:  # bootstrap: seam not importable yet
        backend = resolve_backend_name()
        if backend == "sqlite":
            return _sqlite_bootstrap_shim()
        raise RuntimeError(
            f"observed: db.backend={backend} but the memory package is not "
            f"importable, and only SQLite can be opened without it. "
            f"inspect: put the m3 payload's bin/ on sys.path, or run m3 setup"
        ) from None
    return active_backend()


def _sqlite_bootstrap_shim():
    """seam_backend()'s stand-in when the memory package cannot be imported."""
    import sqlite3 as _sq

    class _SqliteOnlyShim:
        name = "sqlite"
        stores_databases_as_files = True

        @staticmethod
        @contextmanager
        def open_readonly(db_path: str):
            c = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
            try:
                yield c
            finally:
                c.close()

        @staticmethod
        @contextmanager
        def connection(db_path: str | None = None):
            c = _sq.connect(str(db_path or resolve_db_path(None)), timeout=30)
            try:
                yield c
            finally:
                c.close()

    return _SqliteOnlyShim()


def resolve_cli_store(explicit: str | None, flag: str = "--db", default: str | None = None) -> str | None:
    """The store a CLI tool's database argument names, for the active backend.

    File backend: the SQLite path (``explicit`` > ``default`` > store >
    ``M3_DATABASE`` > engine default), which must exist. ``default`` is for a
    store with its own resolver (the chatlog: ``chatlog_db_path()``). Server
    backend: ``None``, since the backend's own DSN locates the one database, and
    an explicit path is refused rather than ignored (a user who passed one
    expects it to be honoured). Raises ValueError carrying observed/inspect; a
    tool prints it and exits 2.
    """
    if store_is_a_file():
        path = resolve_db_path(explicit or default)
        if not os.path.exists(path):
            raise ValueError(f"observed: no SQLite DB at {path}. inspect: {flag}, M3_DATABASE, or M3_ENGINE_ROOT")
        return path
    if explicit:
        raise ValueError(
            f"observed: {flag} names a SQLite file but the active backend is "
            f"{resolve_backend_name()}. inspect: drop {flag}, or `m3 config set db.backend sqlite`"
        )
    return None


def store_is_a_file() -> bool:
    """True when the active backend keeps each database as a file the caller
    names (SQLite), so a path selects the store and a missing file is a missing
    store. False when one server database holds every store (PostgreSQL).

    THE single owner of this question (DESIGN_PHILOSOPHIES §10a). Five tools
    each answered it themselves with three different fallbacks; one of them
    (reembed_space) returned True on ANY error, so a misconfigured PostgreSQL
    install fell into the SQLite file path silently.

    The answer is the backend CLASS's ``stores_databases_as_files``: a fact of
    the backend kind, so asking it builds no instance and needs no DSN. Building
    one made a PostgreSQL selection without a DSN crash here, before a tool could
    give its own refusal ("--database names a SQLite file"). Without the memory
    package (bootstrap, standalone) the configured NAME decides: SQLite is the
    only file backend that can be opened then. Only ImportError falls back; any
    other error from the seam is a configuration fault and propagates (§3).
    """
    try:
        from memory.backends.registry import backend_factory_for
    except ImportError:
        return resolve_backend_name() == "sqlite"
    # The registry validated the name against SELECTABLE_BACKENDS (db.backend's
    # one_of), so it is one of the BackendName literals.
    name = cast("BackendName", resolve_backend_name())
    return bool(backend_factory_for(name).stores_databases_as_files)


# The backend names db.backend may select. `memory.backends.selector` and
# its registry use this tuple; it lives here so tools that must not import the
# `memory` package (migration runners, repair scripts) share the one predicate.
SELECTABLE_BACKENDS: tuple = ("sqlite", "postgres")


def resolve_backend_name() -> str:
    """The configured backend name: registry key ``db.backend``.

    THE single owner of this predicate; ``memory.backends.selector`` re-exports
    it. An ordinary registry key (U9's env override reversed 2026-10-12, m3
    9cbed0af): a ``--set`` flag, then ``[db] backend`` in m3_config.toml, then the
    legacy env names ``M3_DB_BACKEND`` / ``DB_BACKEND`` (below the file, warned
    once per process, until N+3), then ``sqlite``. An unrecognised value raises —
    a typo like ``postgre`` must not silently run SQLite.

    Never memoised: a module-global memo lives per module instance, and two
    loaded instances can disagree about the backend.

    An active named store (``m3_core.config.stores.active_store``) decides first:
    a PostgreSQL store under a SQLite default, or the reverse, is §14's model.
    """
    store = current_store()
    if store is not None:
        return store.backend
    from m3_core.config.resolve import ConfigEnvErrors  # config imports this module
    from m3_core.config.settings import setting

    try:
        name = setting("db.backend")
    except ConfigEnvErrors as e:
        # The selector's contract is ValueError (callers degrade on it); the
        # registry reports every bad env value at once, as a group.
        raise ValueError(f"{e.message}: " + "; ".join(str(x) for x in e.exceptions)) from e
    _warn_backend_from_legacy_env(name)
    return name


_BACKEND_ENV_WARNED: set[str] = set()


def _warn_backend_from_legacy_env(name: str) -> None:
    """Once per process: the backend came from M3_DB_BACKEND / DB_BACKEND, which
    m3 honours for it only until N+3. A daemon that runs on PostgreSQL because
    of a variable nobody sees must say so (§3)."""
    from m3_core.config.settings import current

    origin = current().resolved["db.backend"].origin
    if not origin.startswith("env-legacy:"):
        return
    var = origin.removeprefix("env-legacy:")
    if var in _BACKEND_ENV_WARNED:
        return
    _BACKEND_ENV_WARNED.add(var)
    logger.warning(
        "observed: the storage backend (%s) comes from the environment variable %s, which m3 "
        "reads for it only until release N+3. fix: `m3 config set db.backend %s` (or "
        "`m3 config import-env`), then remove %s where it is set.",
        name,
        var,
        name,
        var,
    )


def require_sqlite_backend(tool: str) -> None:
    """Fail loud if a SQLite-only tool is run against a non-SQLite deployment.

    Such tools open ``sqlite3.connect`` on a file path. Under PostgreSQL that
    file is not the live store, so the tool would silently read or WRITE a stale
    or empty SQLite file. Call this first thing in ``main()``: an operator who
    selected PostgreSQL (``db.backend``) gets an actionable refusal instead. A no-op
    on SQLite (the default).

    Importable without the ``memory`` package, so lightweight bootstrap tools
    can use it.
    """
    name = resolve_backend_name()
    if name != "sqlite":
        raise RuntimeError(
            f"{tool} is SQLite-only; observed: db.backend={name!r}. Running it "
            f"would touch a stale SQLite file, not the live {name} store, so it "
            f"refuses. inspect: run it against a SQLite deployment "
            f"(`m3 config get db.backend` says where the choice came from)"
        )


def snapshot_stores(db_paths, dest_dir, *, label: str) -> list:
    """Verified snapshot of every distinct store a tool is about to write.

    The ONE pre-write backup for the tools that sweep ``db_path`` targets (core,
    then chatlog). Each target is resolved exactly the way those tools resolve
    their WRITES — ``scoped_db_env(path)`` then the active backend — so the copy
    is of the store that will change, never of a file that merely shares its
    name. On SQLite that is each file; on PostgreSQL both targets are one
    database, so ``store_identity`` collapses them to a single dump.

    Returns ``Snapshot`` objects. Raises ``SnapshotError`` on any failure: the
    caller must stop before its first write (DESIGN_PHILOSOPHIES §3, §10).

    Deliberately NOT the bootstrap shim ``seam_backend`` falls back to: a
    snapshot without the seam would be the unverified copy this replaces.
    """
    from memory.backends import active_backend

    seen: set = set()
    out: list = []
    for path in db_paths:
        with scoped_db_env(path):
            backend = active_backend()
            key = backend.store_identity()
            if key in seen:
                continue
            seen.add(key)
            out.append(backend.snapshot(str(dest_dir), label=label))
    return out


def seam_dialect():
    """The active SQL dialect, or a SQLite-shaped default during bootstrap.

    Placeholders are backend-varying (``?`` on SQLite, ``%s`` on PostgreSQL), so
    a literal ``?`` in feature code is a portability bug. Ask for
    ``seam_dialect().param()`` instead. Same bootstrap reasoning as
    :func:`seam_backend`.
    """
    try:
        from memory.backends import dialect
    except ImportError:  # bootstrap: seam not importable yet
        backend = resolve_backend_name()
        if backend == "sqlite":

            class _SqliteParams:
                @staticmethod
                def param() -> str:
                    return "?"

                @staticmethod
                def placeholder(n: int = 1) -> str:
                    return ", ".join("?" * n)

            return _SqliteParams()
        raise RuntimeError(
            f"observed: db.backend={backend} but the memory package is not "
            f"importable, and only SQLite placeholders are known without it. "
            f"inspect: put the m3 payload's bin/ on sys.path, or run m3 setup"
        ) from None
    return dialect()


class _ConfigAction(argparse.Action):
    """``--config FILE``: pin the config file for the whole PROCESS while the
    command line is parsed (``m3_memory._roots.pin_config_file``), so a script
    that resolves a root or a setting after parsing follows it with no code of
    its own, and none can accept the flag and then ignore it."""

    @override
    def __call__(self, parser, namespace, values, option_string=None):
        _roots.pin_config_file(str(values))
        setattr(namespace, self.dest, str(values))


def add_config_arg(parser: argparse.ArgumentParser) -> None:
    """Register ``--config FILE`` on a script's parser: the file its scheduled
    task or service was registered with (``_roots.located_config_args``).
    ``--set`` is not a parser flag: the early pre-parser (``apply_early_argv``)
    reads and removes it before any parser runs."""
    parser.add_argument(
        "--config",
        dest="config_file",
        metavar="FILE",
        default=None,
        action=_ConfigAction,
        help="Read this m3_config.toml (its directory is the config root).",
    )


def apply_early_argv(argv: list[str] | None = None) -> list[str]:
    """THE first statement of an entry script, and the start of `m3`: read
    ``--set={...}`` once (immediately after the program name), apply it to the
    process, remove it from the command line, and pin ``--config``, all BEFORE
    module-level code reads a setting or a root. Returns the command line the
    command parses (``sys.argv`` itself is updated when it was the input). A
    refused ``--set`` stops the program with the reason (exit 2): it never runs
    on a value it was told not to use."""
    import sys

    given = sys.argv if argv is None else argv
    from m3_core.config.settings import OverrideError, apply_set

    try:
        rest = apply_set(given)
    except OverrideError as e:
        print(f"{os.path.basename(given[0]) if given else 'm3'}: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    if given is sys.argv:
        sys.argv[:] = rest
    _roots.pin_from_argv(rest)
    return rest


class _StoreAction(argparse.Action):
    """``--store NAME``: select the named store for the whole PROCESS while the
    command line is parsed (m3_core.config.stores.set_process_store).

    Selecting at parse time is what makes the flag safe on every CLI that takes
    it: a script that resolves its database after parsing follows the store with
    no code of its own, and none can accept the flag and then ignore it. An
    unknown name is an argparse error (exit 2) naming the configured stores.
    """

    @override
    def __call__(self, parser, namespace, values, option_string=None):
        from m3_core.config.stores import StoreError, set_process_store

        try:
            set_process_store(str(values))
        except StoreError as e:
            parser.error(str(e))
        setattr(namespace, self.dest, values)


def deprecated_alias(new_flag: str) -> type[argparse.Action]:
    """An argparse action for the OLD spelling of ``new_flag``: it stores into the
    same dest and says so on stderr every time it is used. (argparse's own
    ``deprecated=`` is 3.13+; the floor is 3.12.)"""

    class _DeprecatedAlias(argparse.Action):
        @override
        def __init__(self, *args, **kwargs):
            # Never a default of its own: argparse seeds a shared dest from the
            # FIRST action registered, so an alias declared before the new flag
            # would replace the new flag's default with None.
            kwargs["default"] = argparse.SUPPRESS
            super().__init__(*args, **kwargs)

        @override
        def __call__(self, parser, namespace, values, option_string=None):
            print(
                f"warning: observed: {option_string} is the old spelling of {new_flag}. "
                f"inspect: use {new_flag}; --store now names a store from m3_config.toml.",
                file=sys.stderr,
            )
            setattr(namespace, self.dest, values)

    return _DeprecatedAlias


def add_database_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the standard --database and --store flags to a CLI parser.

    ``--store NAME`` selects a named store from m3_config.toml for the process
    (its core and files data; config §14, plan 2C.3d) and outranks the legacy
    path. ``--database PATH`` is that legacy SQLite path: --database >
    M3_DATABASE env > default (memory/agent_memory.db); scripts activate it via
    active_database() or by writing os.environ['M3_DATABASE'] before any
    DB-touching code runs. The two are mutually exclusive.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--database",
        default=None,
        metavar="PATH",
        help=("SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db."),
    )
    group.add_argument(
        "--store",
        default=None,
        metavar="NAME",
        action=_StoreAction,
        help="Named store from m3_config.toml ('main' = the default). Not with --database.",
    )
