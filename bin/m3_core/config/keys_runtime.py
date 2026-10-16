"""Registry tables for runtime and services:
[crypto], [mcp], [tools], [dispatch], [identity], [integrations.hermes],
[dashboard], [services], [governor], [gpu], [log], [install], [sync] and
memory safety flags.

Security switches keep today's defaults exactly: FIPS off unless set, wolfSSL
pin unset, destructive tools BLOCKED, agent isolation off, every bind 127.0.0.1.
Today .governor_config.json wins for the GPU probe keys and .log_config.json for
log.max_bytes; they fold in 2D.
"""

from __future__ import annotations

import re

from .registry import (
    ConfigValueError,
    Key,
    in_range,
    one_of,
    parse_bool,
    parse_float,
    parse_int,
    parse_list,
    parse_path,
    parse_str,
)


def _eq1(raw: str) -> bool:
    return raw == "1"


def _yes(raw: str) -> bool:
    return raw.lower() in ("1", "true", "yes")


def _flag(name, env, default, doc, legacy=_eq1):
    return Key[bool](
        name=name, parse=parse_bool, default=default, env_names=env, reloadable=False, legacy_parse=legacy, doc=doc
    )


def _int(name, env, default, doc, lo=1, hi=None):
    return Key[int](
        name=name,
        parse=parse_int,
        default=default,
        validate=in_range(lo=lo, hi=hi),
        env_names=env,
        reloadable=False,
        doc=doc,
    )


def _float(name, env, default, doc):
    return Key[float](
        name=name, parse=parse_float, default=default, validate=in_range(lo=0), env_names=env, reloadable=False, doc=doc
    )


def _str(name, env, default, doc):
    return Key[str](name=name, parse=parse_str, default=default, env_names=env, reloadable=False, doc=doc)


def _upper(raw: str) -> str:
    return raw.strip().upper()


def _lower(raw: str) -> str:
    return raw.strip().lower()


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha256_hex(value: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ConfigValueError("must be a 64-character lowercase hex SHA-256 digest")


RUNTIME: list[Key] = [
    # ── [crypto] (crypto_provider.py; read at import) ──
    _flag(
        "crypto.fips_mode",
        ("M3_FIPS_MODE",),
        False,
        "FIPS 140-3 mode: wolfSSL/wolfCrypt is required and any failure is fatal.",
    ),
    _flag(
        "crypto.fips_strict",
        ("M3_FIPS_STRICT",),
        False,
        "Also require the CMVP-validated wolfCrypt build (implies the wolfSSL backend).",
    ),
    _flag(
        "crypto.tls_validated",
        (),
        False,
        "Under crypto.fips_strict, attest that network TLS (Python's ssl and PostgreSQL's libpq) runs on a "
        "FIPS 140-3 validated library, e.g. an OS FIPS OpenSSL provider. m3 cannot verify this itself; without "
        "it, strict mode refuses network TLS. See docs/FIPS_MODULE_BOUNDARY.md.",
    ),
    Key[str](
        name="crypto.backend",
        parse=_upper,
        default="DEFAULT",
        validate=one_of("DEFAULT", "WOLFSSL"),
        env_names=("M3_CRYPTO_BACKEND",),
        reloadable=False,
        doc="Crypto backend: DEFAULT or WOLFSSL (forced by crypto.fips_mode / fips_strict). "
        "(Today an unknown value acts as DEFAULT; it is now an error.)",
    ),
    Key(
        name="crypto.wolfssl_lib",
        parse=parse_path,
        default=None,
        env_names=("M3_WOLFSSL_LIB",),
        reloadable=False,
        doc="The wolfSSL library file tried first; unset searches the trusted lib dirs.",
    ),
    Key[str](
        name="crypto.wolfssl_sha256",
        parse=_lower,
        default=None,
        validate=_sha256_hex,
        env_names=("M3_WOLFSSL_SHA256",),
        reloadable=False,
        doc="Integrity pin for the wolfSSL library; checked when wolfSSL initialises.",
    ),
    # ── [mcp] / [tools] / [dispatch] ──
    _flag(
        "mcp.allow_destructive",
        ("M3_MCP_PROXY_ALLOW_DESTRUCTIVE", "MCP_PROXY_ALLOW_DESTRUCTIVE"),
        False,
        "Let proxies run catalog tools marked not default-allowed. Off: they are blocked.",
        legacy=lambda raw: (raw or "").lower() in ("1", "true", "yes"),
    ),
    _str("mcp.proxy_host", ("M3_MCP_PROXY_HOST", "MCP_PROXY_HOST"), "127.0.0.1", "Address the MCP proxy binds."),
    # One parser for every reader (the bridge, tools_config): a per-reader parse split
    # them on "false" / "no" / "off".
    _flag("tools.lazy", ("M3_TOOLS_LAZY",), True, "Load MCP tool domains on demand."),
    Key[list[str]](
        name="tools.startup",
        parse=parse_list,
        default=None,
        env_names=("M3_TOOLS_STARTUP",),
        reloadable=False,
        doc="MCP tools registered at session start (must include the escape hatch). Unset: "
        ".tools_config.json startup_tools, else the shipped set.",
    ),
    Key[float](
        name="tools.timeout_s",
        parse=parse_float,
        default=None,
        env_names=("M3_TOOL_TIMEOUT",),
        reloadable=False,
        doc="Seconds a tool call may take; 0 or less disables. Unset: each tool's own, else 30.",
    ),
    Key[str](
        name="dispatch.mode",
        parse=_lower,
        default="separate",
        validate=one_of("separate", "integrated"),
        env_names=("M3_DISPATCH_MODE",),
        reloadable=False,
        doc="Dispatch queue store: separate (its own) or integrated (the main store). "
        "(Today an unknown value acts as separate; it is now an error.)",
    ),
    # ── [memory] safety ──
    _flag(
        "memory.enforce_agent_isolation",
        ("M3_ENFORCE_AGENT_ISOLATION",),
        False,
        "Restrict a search to the requesting agent's memories.",
    ),
    _flag(
        "memory.disable_auto_activation",
        ("M3_DISABLE_AUTO_ACTIVATION",),
        False,
        "Never switch features on automatically from data volume (an explicit flag still wins).",
        legacy=lambda raw: raw.strip().lower() in ("1", "true", "yes", "on"),
    ),
    # ── [identity] ──
    _str(
        "identity.node_id",
        ("M3_NODE_ID",),
        None,
        "Sync node identity. Default: the host name (upper-cased on Windows).",
    ),
    _str(
        "identity.origin_device",
        ("M3_ORIGIN_DEVICE", "ORIGIN_DEVICE"),
        None,
        "Device name stamped on memories. Default: the host name.",
    ),
    _str(
        "identity.default_user_id",
        ("M3_DEFAULT_USER_ID",),
        None,
        "user_id the LangChain / mem0 surfaces use when a caller passes none (single-user "
        "prototyping). Unset: they raise; isolation is never weakened.",
    ),
    # ── [integrations.hermes] (a $HERMES_HOME/m3.json wins today) ──
    _str("integrations.hermes.agent_id", ("M3_AGENT_ID",), "hermes", "Agent id the Hermes plugin writes as."),
    _str("integrations.hermes.user_id", ("M3_USER_ID",), "hermes-user", "User id the Hermes plugin writes as."),
    Key(
        name="integrations.hermes.home",
        parse=parse_path,
        default=None,
        env_names=("M3_HERMES_HOME",),
        reloadable=False,
        doc="Hermes Agent's home; unset uses HERMES_HOME, then probed locations.",
    ),
    # ── [dashboard] / [services] ──
    _str("dashboard.host", ("M3_DASHBOARD_HOST",), "127.0.0.1", "Address the dashboard binds."),
    _int("dashboard.port", ("M3_DASHBOARD_PORT",), 8088, "Dashboard port.", hi=65535),
    _flag(
        "dashboard.llm_smoke",
        ("M3_DASHBOARD_LLM_SMOKE",),
        False,
        "Let the dashboard send a small test prompt to each LLM endpoint.",
        legacy=_yes,
    ),
    _float(
        "dashboard.llm_smoke_ttl_s", ("M3_DASHBOARD_LLM_SMOKE_TTL_S",), 900.0, "Seconds an LLM smoke result is cached."
    ),
    _float(
        "dashboard.llm_block_ttl_s",
        ("M3_DASHBOARD_LLM_BLOCK_TTL_S",),
        45.0,
        "Seconds an endpoint that failed the smoke test is skipped.",
    ),
    _str(
        "services.macbook_status_host",
        ("M3_MACBOOK_STATUS_HOST", "MACBOOK_STATUS_HOST"),
        "127.0.0.1",
        "Address the macbook status server binds.",
    ),
    # ── [governor] / [gpu] / [log] / [context] ──
    _float(
        "governor.config_ttl_s",
        ("M3_GOVERNOR_CFG_TTL",),
        5.0,
        "Seconds between checks of .governor_config.json for changes.",
    ),
    _int("loop.throttled_limit", ("M3_GOVERNOR_THROTTLED_LIMIT",), 1, "Items per loop pass under THROTTLED load."),
    # Thresholds read through governor wrappers the env gate could not see until
    # 2026-10-09 (governor.py:137, :176). Today out-of-range values are CLAMPED
    # silently; the registry rejects them (intended-change list). Order: this table >
    # .governor_config.json > the env name > default (governor.py).
    _int(
        "governor.initial_threshold",
        ("M3_GOVERNOR_INITIAL_THRESHOLD",),
        85,
        "Load percent at which the governor starts throttling.",
        lo=10,
        hi=99,
    ),
    _int(
        "governor.limit_threshold",
        ("M3_GOVERNOR_LIMIT_THRESHOLD",),
        95,
        "Load percent at which the governor halts background work.",
        lo=20,
        hi=100,
    ),
    _float(
        "governor.ram_halt_gb", ("M3_GOVERNOR_RAM_HALT_GB",), 1.0, "Free RAM (GB) below which background work halts."
    ),
    _float(
        "governor.ram_throttle_gb",
        ("M3_GOVERNOR_RAM_THROTTLE_GB",),
        4.0,
        "Free RAM (GB) below which background work throttles.",
    ),
    _float(
        "governor.ram_throttle_idle_gb",
        ("M3_GOVERNOR_RAM_THROTTLE_IDLE_GB",),
        2.0,
        "Free RAM (GB) below which work throttles while the user is idle.",
    ),
    _flag("gpu.probe_disabled", ("M3_GPU_PROBE_DISABLE",), False, "Do not probe GPU load.", legacy=_yes),
    _float("gpu.probe_ttl_s", ("M3_GPU_PROBE_TTL",), 60.0, "Seconds a GPU probe result is cached."),
    _int(
        "log.max_bytes",
        ("M3_LOG_MAX_BYTES",),
        64 * 1024 * 1024,
        "Task log size that triggers rotation; 0 disables rotation.",
        lo=0,
    ),
    Key(
        name="log.task_log_file",
        parse=parse_path,
        default=None,
        env_names=("M3_TASK_LOG_FILE",),
        reloadable=False,
        doc="A scheduled task's log file. Default: <logs root>/<task>.log.",
    ),
    _int("context.cache_size", ("M3_CONTEXT_CACHE_SIZE",), 16, "M3Context instances kept per process.", lo=2),
    # ── [install] ──
    _flag(
        "install.auto_install",
        ("M3_AUTO_INSTALL",),
        True,
        "Let `m3` install a missing payload on first run.",
        legacy=lambda raw: raw.strip() != "0",
    ),
    _flag("install.no_model_download", ("M3_NO_MODEL_DOWNLOAD",), False, "Never download embedding model weights."),
    _int(
        "install.os_install_timeout_s",
        ("M3_OS_INSTALL_TIMEOUT_S",),
        180,
        "Seconds an unattended OS package install may take.",
    ),
    # ── [sync]: pg_sync to the warehouse ──
    _int("sync.timeout_s", ("M3_PG_SYNC_TIMEOUT",), 600, "Seconds one warehouse sync may take."),
    _str(
        "sync.warehouse_host",
        ("M3_POSTGRES_SERVER", "POSTGRES_SERVER"),
        None,
        "Warehouse host. Default: sync.target_ip, then the warehouse DSN's host.",
    ),
    _str("sync.target_ip", ("M3_SYNC_TARGET_IP", "SYNC_TARGET_IP"), None, "Warehouse address (legacy form)."),
]
