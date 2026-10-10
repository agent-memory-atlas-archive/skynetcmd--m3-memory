"""Registry table [embed] (plan 2C.1, table 2; CONFIG_DESIGN §6, U24).

Defaults are compared with what today's readers produce: their module
constants, imported in a FRESH interpreter with every registry env name
removed (the constants are bound at import, so an in-process import could hold
an earlier test's values).
"""

from __future__ import annotations

import logging

import pytest

# registry key -> (module, constant) holding today's default
_PINNED = {
    "embed.model": ("memory.config", "EMBED_MODEL"),
    "embed.dim": ("memory.config", "EMBED_DIM"),
    "embed.require_unit_norm": ("memory.config", "EMBED_REQUIRE_UNIT_NORM"),
    "embed.norm_tolerance": ("memory.config", "EMBED_NORM_TOL"),
    "embed.search_deadline_s": ("memory.config", "EMBED_SEARCH_DEADLINE_S"),
    "embed.compatible_models": ("memory.config", "EMBED_COMPATIBLE_MODELS"),
    "embed.breaker.embedded_threshold": ("memory.config", "EMBED_BREAKER_EMBEDDED_THRESHOLD"),
    "embed.breaker.embedded_reset_s": ("memory.config", "EMBED_BREAKER_EMBEDDED_RESET_SECS"),
    "embed.breaker.cpu_fallback_threshold": ("memory.config", "EMBED_BREAKER_CPU_FALLBACK_THRESHOLD"),
    "embed.breaker.cpu_fallback_reset_s": ("memory.config", "EMBED_BREAKER_CPU_FALLBACK_RESET_SECS"),
    "embed.breaker.primary_threshold": ("memory.config", "EMBED_BREAKER_PRIMARY_THRESHOLD"),
    "embed.breaker.primary_reset_s": ("memory.config", "EMBED_BREAKER_PRIMARY_RESET_SECS"),
    "embed.breaker.cloud_threshold": ("memory.config", "EMBED_BREAKER_CLOUD_THRESHOLD"),
    "embed.breaker.cloud_reset_s": ("memory.config", "EMBED_BREAKER_CLOUD_RESET_SECS"),
    "embed.http.max_connections": ("memory.embed", "_EMBED_HTTP_MAX_CONNS"),
    "embed.http.max_keepalive": ("memory.embed", "_EMBED_HTTP_MAX_KEEPALIVE"),
    "embed.bulk_chunk": ("memory.embed", "EMBED_BULK_CHUNK"),
    "embed.bulk_concurrency": ("memory.embed", "EMBED_BULK_CONCURRENCY"),
    "embed.entity_name_cache_max": ("memory.embed", "ENTITY_NAME_EMBED_CACHE_MAX"),
    "embed.token_budget": ("memory.tokens", "TOKEN_BUDGET"),
    "embed.token_overlap": ("memory.tokens", "TOKEN_OVERLAP"),
    "embed.chunk_max_chars": ("memory.chunking", "MAX_CHARS_PER_CHUNK"),
    "embed.chunk_overlap_chars": ("memory.chunking", "MIN_OVERLAP_CHARS"),
    "embed.server.max_batch": ("embed_server_inproc", "_MAX_BATCH"),
    "embed.ctx": ("embed_server_inproc", "_EMBED_CTX"),
    "embed.server.interactive_max_texts": ("embed_server_inproc", "_INTERACTIVE_MAX_TEXTS"),
    "embed.server.interactive_reserved": ("embed_server_inproc", "_INTERACTIVE_RESERVED"),
    "embed.server.concurrency": ("embed_server_inproc", "_ADMISSION_TOTAL_DEFAULT"),
}


def test_defaults_are_what_todays_readers_produce():
    from config_defaults_helper import mismatches

    wrong = mismatches(_PINNED)
    assert not wrong, f"registry default != today's reader (registry, today): {wrong}"


def _build(tmp_path, environ):
    from m3_core.config import Location
    from m3_core.config.settings import build

    return build(Location(file=tmp_path / "m3_config.toml", root=tmp_path, tier="test"), environ=environ)


@pytest.mark.parametrize(
    "env,raw,today,now",
    [
        ("M3_EMBED_INPROC", "true", False, True),  # today only "1" enabled it
        ("M3_EMBED_GGUF_AUTODETECT", "false", True, False),  # today only "0" disabled it
        ("M3_EMBED_REQUIRE_UNIT_NORM", "off", True, False),  # today "off" was not a false value
    ],
)
def test_a_changed_reading_is_logged_once(tmp_path, caplog, env, raw, today, now):
    from m3_core.config import REGISTRY
    from m3_core.config import resolve as r

    r._LEGACY_NOTED.discard((env, raw))
    key = REGISTRY.by_env(env)
    with caplog.at_level(logging.WARNING):
        s1 = _build(tmp_path, {env: raw})
        s2 = _build(tmp_path, {env: raw})
    assert key.legacy_parse(raw) is today and s1[key] is now and s2[key] is now
    assert caplog.text.count(f"{env}={raw!r} now reads as {now!r}") == 1, caplog.text


def test_an_agreeing_value_is_not_logged(tmp_path, caplog):
    from m3_core.config import REGISTRY

    with caplog.at_level(logging.WARNING):
        s = _build(tmp_path, {"M3_EMBED_INPROC": "1"})
    assert s[REGISTRY.by_env("M3_EMBED_INPROC")] is True and "now reads as" not in caplog.text


def test_the_sweeper_deadline_accepts_unbounded(tmp_path):
    from m3_core.config import REGISTRY

    assert _build(tmp_path, {"M3_CHATLOG_EMBED_DEADLINE_S": "-1"})[REGISTRY.get("chatlog.embed.deadline_s")] == -1.0


def test_gguf_is_not_an_env_override():
    """User decision (m3 a24fa878): a per-process override splits the embedders."""
    from m3_core.config import REGISTRY

    assert REGISTRY.get("embed.gguf").env_override is False
