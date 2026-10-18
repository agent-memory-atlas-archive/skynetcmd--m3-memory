"""The registered keys, table by table.

Each table lands in its own commit with its own tests. A name the env scanner
finds that is in no table yet is listed in ``tests/data/config_unregistered.txt``,
and that list may only shrink (tests/test_config_registry_complete.py).
"""

from __future__ import annotations

from m3_core.paths import SELECTABLE_BACKENDS

from .keys_chatlog import CHATLOG
from .keys_embed import EMBED
from .keys_enrich import ENRICH
from .keys_files import FILES
from .keys_llm import LLM
from .keys_runtime import RUNTIME
from .keys_search import SEARCH
from .registry import Key, KeyClass, Registry, Scope, in_range, one_of, parse_int, parse_list, parse_path, parse_str


def _parse_backend(raw: str) -> str:
    # Today's reading exactly (paths.resolve_backend_name): strip, lowercase.
    return raw.strip().lower()


# ── [db] ─────────────────────────────────────────────────────────────────────
DB_BACKEND = Key[str](
    name="db.backend",
    parse=_parse_backend,
    default="sqlite",
    validate=one_of(*SELECTABLE_BACKENDS),  # S6: the selector owns the set
    env_names=("M3_DB_BACKEND", "DB_BACKEND"),
    reloadable=False,
    doc="Primary storage backend. Set it with `m3 config set db.backend postgres`; "
    "the former names M3_DB_BACKEND and DB_BACKEND are not read.",
)

_POSITIVE = in_range(lo=1)

DB: list[Key] = [
    DB_BACKEND,
    # m3_core/context.py:159-160 (getenv_compat, int()).
    Key[int](
        name="db.pool_size",
        parse=parse_int,
        default=5,
        validate=_POSITIVE,
        env_names=("M3_DB_POOL_SIZE", "DB_POOL_SIZE"),
        reloadable=False,
        doc="SQLite connections in M3Context's pool.",
    ),
    Key[int](
        name="db.pool_timeout_s",
        parse=parse_int,
        default=30,
        validate=_POSITIVE,
        env_names=("M3_DB_POOL_TIMEOUT", "DB_POOL_TIMEOUT"),
        reloadable=False,
        doc="Seconds to wait for a pooled SQLite connection.",
    ),
    # sqlite_pragmas.py:91,156: today a non-integer is SILENTLY ignored; the
    # registry rejects it (intended-change list).
    Key[int](
        name="db.sqlite_mmap_size",
        parse=parse_int,
        default=None,
        validate=in_range(lo=0),
        env_names=("M3_SQLITE_MMAP_SIZE",),
        reloadable=False,
        doc="SQLite mmap_size in bytes; unset keeps the pragma profile's own value.",
    ),
]

DB_POSTGRES: list[Key] = [
    # postgres_backend.py:938-939 (int(v or N)); min <= max is checked there.
    Key[int](
        name="db.postgres.pool_min",
        parse=parse_int,
        default=1,
        validate=_POSITIVE,
        env_names=("M3_PG_POOL_MIN",),
        reloadable=False,
        doc="Minimum connections in the PostgreSQL pool.",
    ),
    Key[int](
        name="db.postgres.pool_max",
        parse=parse_int,
        default=16,
        validate=_POSITIVE,
        env_names=("M3_PG_POOL_MAX",),
        reloadable=False,
        doc="Maximum connections in the PostgreSQL pool (at least pool_min).",
    ),
    Key[int](
        name="db.postgres.upsert_page_size",
        parse=parse_int,
        default=500,
        validate=_POSITIVE,
        env_names=("M3_PG_UPSERT_PAGE_SIZE",),
        doc="Rows per bulk upsert page; lower it over a slow link (a deliberate tuning knob).",
    ),
    Key[list[str]](
        name="db.postgres.forbidden_hosts",
        parse=parse_list,
        default=[],
        env_names=("M3_PG_FORBIDDEN_HOSTS",),
        reloadable=False,
        doc="Hosts a PRIMARY store must never connect to (e.g. the warehouse); selecting one is refused.",
    ),
    Key(
        name="db.postgres.pg_dump",
        parse=parse_path,
        default=None,
        env_names=("M3_PG_DUMP",),
        doc="pg_dump used for snapshots; unset finds it on PATH. pg_restore is looked for beside it.",
    ),
    Key(
        name="db.postgres.pg_restore",
        parse=parse_path,
        default=None,
        env_names=("M3_PG_RESTORE",),
        doc="pg_restore used to verify snapshots; unset: beside pg_dump, else on PATH.",
    ),
    Key[str](
        name="db.postgres.dispatch_schema",
        parse=parse_str,
        default="m3_dispatch",
        env_names=("M3_DISPATCH_PG_SCHEMA",),
        reloadable=False,
        doc="Schema holding the dispatch queue on PostgreSQL.",
    ),
]

# ── [paths] (the roots MCP server blocks pin today) ──────────
# Defaults are derived (engine root = <memory root>/engine, else ~/.m3/engine),
# so the key's default is None and m3_core.paths stays the resolver until 2D
# converts its readers. All are fixed for a process's lifetime.
PATHS: list[Key] = [
    Key(
        name="paths.memory_root",
        parse=parse_path,
        default=None,
        env_names=("M3_MEMORY_ROOT",),
        scope=Scope.LOCATOR,
        reloadable=False,
        doc="Master root: the engine, logs and models roots derive from it when their own keys "
        "are unset. The config root never does (M3_CONFIG_ROOT or ~/.m3/config locates it).",
    ),
    Key(
        name="paths.engine_root",
        parse=parse_path,
        default=None,
        env_names=("M3_ENGINE_ROOT",),
        scope=Scope.LOCATOR,
        reloadable=False,
        doc="Databases and runtime state. Default: <memory_root>/engine, else ~/.m3/engine.",
    ),
    Key(
        name="paths.logs_root",
        parse=parse_path,
        default=None,
        env_names=("M3_LOGS_ROOT",),
        scope=Scope.LOCATOR,
        reloadable=False,
        doc="Scheduled-task and service logs. Default: <memory_root>/logs, else ~/.m3/logs.",
    ),
    Key(
        name="paths.models_root",
        parse=parse_path,
        default=None,
        env_names=("M3_MODELS_ROOT",),
        scope=Scope.LOCATOR,
        reloadable=False,
        doc="GGUF weights m3 owns. Default: <memory_root>/models, else ~/.m3/models.",
    ),
    Key(
        name="paths.bin",
        parse=parse_path,
        default=None,
        env_names=("M3_PATH_BIN",),
        scope=Scope.LOCATOR,
        reloadable=False,
        doc="The bin/ directory of a non-packaged (developer) install; unset uses the packaged one.",
    ),
    *[
        Key(
            name=f"paths.{part}",
            parse=parse_path,
            default=None,
            env_names=(env,),
            scope=Scope.LOCATOR,
            reloadable=False,
            doc=f"The payload's {sub}/ directory for a non-packaged install "
            f"(installer.py _payload_dir); unset uses the packaged one.",
        )
        for part, env, sub in (
            ("assets", "M3_PATH_ASSETS", "_assets"),
            ("docs", "M3_PATH_DOC", "docs"),
            ("examples", "M3_PATH_EXAMPLES", "examples"),
        )
    ],
    Key(
        name="paths.lib_dir",
        parse=parse_path,
        default=None,
        env_names=("M3_LIB_DIR",),
        scope=Scope.LOCATOR,
        reloadable=False,
        doc="Where the wolfSSL library is trusted from. Default: <config root's parent>/lib.",
    ),
]

# ── [secrets] ────────────────────────────────────────────────────────────────
SECRETS_FILE = Key(
    name="secrets.file",
    parse=parse_path,
    default=None,  # <config root>/m3_secrets.toml, resolved by m3_core.secret_store
    scope=Scope.LOCATOR,
    reloadable=False,
    doc="The user-only secrets file: every m3 secret, as its value or a pointer to its "
    "OS keyring entry. Default: <config root>/m3_secrets.toml.",
)


def _secret(name: str, doc: str, *, aliases: tuple[str, ...] = (), bootstrap: bool = False) -> Key:
    return Key[str](
        name=name,
        parse=parse_str,
        default=None,
        klass=KeyClass.SECRET,
        bootstrap=bootstrap,
        env_names=(name, *aliases),
        doc=doc,
    )


# Bootstrap secrets never come from the vault: it lives in the
# store a DSN unlocks, and it decrypts with the master key.
SECRETS: list[Key] = [
    _secret("M3_PRIMARY_PG_URL", "DSN of a PostgreSQL primary store.", aliases=("M3_PG_URL",), bootstrap=True),
    _secret("M3_CDW_PG_URL", "DSN of the pg_sync data warehouse.", aliases=("PG_URL",)),
    _secret("AGENT_OS_MASTER_KEY", "Key that decrypts the vault.", bootstrap=True),
    _secret("M3_AGENT_OS_SALT_HEX", "The vault's device salt.", bootstrap=True),
    _secret("LM_API_TOKEN", "Token for the local LLM endpoint.", aliases=("LM_STUDIO_API_KEY",)),
    _secret("ANTHROPIC_API_KEY", "Anthropic API key."),
    _secret("OPENAI_API_KEY", "OpenAI API key."),
    _secret("GEMINI_API_KEY", "Gemini API key."),
    _secret("M3_CLOUD_AUTH_TOKEN", "Bearer token for the tier-4 cloud embedder."),
    _secret("M3_SERVE_TOKEN", "Bearer token `m3 serve` requires."),
    _secret(
        "M3_EMBED_SERVER_TOKEN",
        "Bearer token for the shared embed server's listener for other machines (`m3 embedder secure`).",
    ),
    # Read by m3-embed-server from the secrets file (plain value; a keyring
    # pointer is refused there), only when embed.server.remote_listen is set.
    _secret(
        "M3_EMBED_SERVER_TOKEN_SHA256",
        "SHA-256 (hex) of M3_EMBED_SERVER_TOKEN; m3-embed-server checks bearer tokens against it "
        "(written by `m3 embedder secure`).",
    ),
    _secret("OPENCLAW_GATEWAY_TOKEN", "OpenClaw gateway token."),
    _secret("MCP_PROXY_KEY", "Shared token the MCP proxy requires; unset runs it without auth (warned)."),
    _secret("XAI_API_KEY", "xAI API key (the connect_xai_grok profile)."),
]

REGISTRY = Registry(
    [*DB, *DB_POSTGRES, *PATHS, *EMBED, *FILES, *SEARCH, *LLM, *ENRICH, *CHATLOG, *RUNTIME, SECRETS_FILE, *SECRETS]
)

# Env names m3 code reads that are deliberately NOT settings, with the reason.
# The completeness test treats them as decided; only the user moves a name here.
# Documented settings that do NOTHING today: no reader, a reader nothing calls,
# or a value every caller overrides with the same literal. Retired by the user
# 2026-10-09 ("Retire them"): never registered; `m3 config show` and doctor say
# a retired name that is set has no effect; the docs promising them are fixed.
RETIRED: dict[str, str] = {
    "M3_FILES_DEFAULT_SCOPE": "no reader (files_memory/config.py:84)",
    "M3_FILES_EXTRACT_CONCURRENCY": "no reader (files_memory/config.py:111)",
    "M3_FILES_EXTRACT_MAX_ATTEMPTS": "no reader (files_memory/config.py:116)",
    "M3_FILES_DB_PROMPT_ON_FIRST_USE": "no reader (memory/config.py:101)",
    "M3_FILES_CORPUS": "its reader has no production caller (files_memory/corpora.py:73)",
    "M3_FILES_DEDUP_THRESHOLD": "every caller passes 0.92 (files_memory/dedup.py:53)",
    "M3_FILES_DEDUP_MAX_PAIRS": "every caller passes 500 (files_memory/dedup.py:54)",
    "M3_FILES_DEDUP_LEAF_LIMIT": "every caller passes 10000 (files_memory/dedup.py:55)",
    "M3_FILES_PROMO_SUGGEST_THRESHOLD": "every caller passes 0.30 (files_memory/promotability.py:43)",
    "M3_CONFIDENCE_MODEL": "no consumer (memory/config.py:252)",
    "M3_TRUST_AUTOTUNE": "no consumer (memory/config.py:265)",
    "M3_CLOUD_MINIMIZATION_LEVEL": "no consumer (memory/config.py:422)",
    "M3_ENTITY_COALESCE_MAX_PAIRS": "every caller passes 1000 (files_memory/entity_coalesce.py:69)",
    "CHATLOG_MODE": "only its presence is checked, to warn (chatlog_config.py:257)",
    "M3_ROUTE_SHADOW_MODE": "route shadow mode was retired in 2026.10.17.3: the native router is used when present",
}

NOT_SETTINGS: dict[str, str] = {
    # Dev-tool parameters.
    "M3_DIFF_REF_PYTHON": "search_differential.py's own parameter",
    "M3_DIFF_REF_BIN": "search_differential.py's own parameter",
    "M3_DIFF_REF_REV": "search_differential.py's own parameter",
    "M3_DIFF_STORE": "search_differential.py's own parameter",
    "M3_TRACE_FILE": "setup-wizard debug breadcrumbs",
    # install_os.py runs before m3 (and its config) is installed.
    "M3_INSTALL_OXIDATION": "install_os.py's unattended answer to the native-core prompt",
    # It decides WHICH m3_config.toml is read, so it cannot be a key in that file.
    "M3_CONFIG_ROOT": "the config locator: it picks which m3_config.toml is read",
    # The chatlog hooks (Python and shell) read it to
    # find bin/ before any m3 code runs, so it cannot come from the config file.
    "M3_HOME": "the hooks' bootstrap locator for the code checkout",
    # A diagnostic switch, read before config loads.
    "M3_DEBUG": "the diagnostic switch, read before config loads",
    # Retired as a user setting, no env read and no TOML key; one
    # Rust-availability probe replaces its parse sites.
    "M3_CORE_RS_DISABLE": "retired as a user setting; tests use a fixture",
    # The Rust/Python route-decider switch is dev and
    # bench plumbing, folded into the test-only Rust-availability seam.
    # Read only by a live judge test (test plumbing, not settings).
    "M3_WIKI_DRIFT_PRECISION_FLOOR": "test plumbing: the drift judge live test (wiki/citation_drift.py:183)",
    "M3_WIKI_DRIFT_RECALL_FLOOR": "test plumbing: the drift judge live test (wiki/citation_drift.py:182)",
    # In-process handoffs from `m3 serve` to the bridge, which are to become
    # explicit parameters (today m3 serve overwrites them every run).
    "M3_TRANSPORT": "in-process handoff; 2E makes it a parameter",
    "M3_HTTP_HOST": "in-process handoff; 2E makes it a parameter",
    "M3_HTTP_PORT": "in-process handoff; 2E makes it a parameter",
    "M3_HTTP_PATH": "in-process handoff; 2E makes it a parameter",
    "M3_HTTP_PUBLIC_HOST": "in-process handoff; 2E makes it a parameter",
    "_M3_UTF8_REEXEC": "internal guard against re-executing twice for UTF-8 mode",
    # As GH_TOKEN: release tooling only.
    "M3_STARGAZER": "release tooling token (gen_download_badges.py; as GITHUB_TOKEN)",
    # A test hook with no production writer.
    "M3_SKIP_MIGRATIONS": "test hook: skip schema migrations (memory/db.py:282)",
}
