"""Registry tables [llm], [cloud], [extraction], [promote]."""

from __future__ import annotations

_PINNED = {
    "llm.timeout_s": ("memory.config", "LLM_TIMEOUT"),
    "llm.read_timeout_s": ("m3_core.runtime", "LM_READ_TIMEOUT"),
    "llm.lmstudio_base": ("m3_core.runtime", "LM_STUDIO_BASE"),
    "promote.llm_url": ("promote_pipeline", "LM_URL"),
    "promote.llm_model": ("promote_pipeline", "LM_MODEL"),
    "cloud.allow_fallback": ("memory.config", "M3_ALLOW_CLOUD_FALLBACK"),
    "cloud.enclave_url": ("memory.config", "M3_CLOUD_ENCLAVE_URL"),
    "cloud.auth_token_keyring": ("memory.config", "M3_CLOUD_AUTH_TOKEN_KEYRING"),
}


def test_defaults_are_what_todays_readers_produce():
    from config_defaults_helper import mismatches

    wrong = mismatches(_PINNED)
    assert not wrong, f"registry default != today's reader (registry, today): {wrong}"


def test_the_proxy_reads_the_same_defaults_as_runtime():
    """mcp_proxy.py:141,149 read the same two names; one key covers both."""
    from config_defaults_helper import mismatches

    assert not mismatches(
        {"llm.lmstudio_base": ("mcp_proxy", "LMSTUDIO_BASE"), "llm.read_timeout_s": ("mcp_proxy", "READ_TIMEOUT")}
    )


def test_the_keyring_reference_is_config_and_the_token_is_a_secret():
    from m3_core.config import REGISTRY, KeyClass

    assert REGISTRY.get("cloud.auth_token_keyring").klass is KeyClass.CONFIG
    assert REGISTRY.get("M3_CLOUD_AUTH_TOKEN").klass is KeyClass.SECRET


def test_unused_minimization_level_is_retired():
    from m3_core.config import REGISTRY
    from m3_core.config.keys import RETIRED

    assert REGISTRY.by_env("M3_CLOUD_MINIMIZATION_LEVEL") is None and "M3_CLOUD_MINIMIZATION_LEVEL" in RETIRED
