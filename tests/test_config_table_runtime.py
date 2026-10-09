"""Runtime and service tables (plan 2C.1). Security defaults are pinned exactly."""
from __future__ import annotations

import pytest

_PINNED = {
    "dashboard.host": ("dashboard_server", "HOST"),
    "dashboard.port": ("dashboard_server", "PORT"),
    "dashboard.llm_smoke_ttl_s": ("dashboard.health", "_SMOKE_TTL_S"),
    "dashboard.llm_block_ttl_s": ("dashboard.health", "_BLOCK_TTL_S"),
    "mcp.proxy_host": ("mcp_proxy", "PROXY_HOST"),
    "mcp.allow_destructive": ("mcp_proxy", "ALLOW_DESTRUCTIVE"),
    "memory.enforce_agent_isolation": ("memory.config", "ENFORCE_AGENT_ISOLATION"),
    "governor.config_ttl_s": ("m3_core.governor", "_GOV_CFG_TTL"),
    "gpu.probe_disabled": ("m3_core.gpu", "_GPU_PROBE_DISABLE"),
    "gpu.probe_ttl_s": ("m3_core.gpu", "_GPU_PROBE_TTL"),
    "log.max_bytes": ("_task_runtime", "_DEFAULT_LOG_MAX_BYTES"),
    "context.cache_size": ("m3_core.context", "_CONTEXT_CACHE_SIZE"),
    "sync.timeout_s": ("sync_all", "PG_SYNC_TIMEOUT"),
}


def test_defaults_are_what_todays_readers_produce():
    from config_defaults_helper import mismatches

    wrong = mismatches(_PINNED)
    assert not wrong, f"registry default != today's reader (registry, today): {wrong}"


def test_security_defaults_are_unchanged():
    """Destructive tools blocked, isolation off, FIPS off, no pin, loopback binds."""
    from m3_core.config import REGISTRY

    expect = {"mcp.allow_destructive": False, "memory.enforce_agent_isolation": False,
              "crypto.fips_mode": False, "crypto.fips_strict": False, "crypto.backend": "DEFAULT",
              "crypto.wolfssl_sha256": None, "dashboard.host": "127.0.0.1", "mcp.proxy_host": "127.0.0.1",
              "services.macbook_status_host": "127.0.0.1"}
    assert {k: REGISTRY.get(k).default for k in expect} == expect


def test_the_wolfssl_pin_must_be_a_digest():
    from m3_core.config import REGISTRY, ConfigValueError

    key = REGISTRY.get("crypto.wolfssl_sha256")
    key.check("a" * 64)
    with pytest.raises(ConfigValueError, match="SHA-256"):
        key.check("not-a-digest")


def test_handoffs_and_tooling_are_not_settings():
    from m3_core.config import REGISTRY
    from m3_core.config.keys import NOT_SETTINGS

    for env in ("M3_TRANSPORT", "M3_HTTP_HOST", "M3_HTTP_PORT", "M3_HTTP_PATH", "M3_HTTP_PUBLIC_HOST",
                "_M3_UTF8_REEXEC", "M3_STARGAZER", "M3_SKIP_MIGRATIONS"):
        assert env in NOT_SETTINGS and REGISTRY.by_env(env) is None, env
