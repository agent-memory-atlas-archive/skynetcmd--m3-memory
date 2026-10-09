"""Registry table [files] (plan 2C.1). Defaults pinned to today's constants."""
from __future__ import annotations

import logging

_PINNED = {
    "files.default_corpus": ("files_memory.config", "FILES_DEFAULT_CORPUS"),
    "files.default_extract_mode": ("files_memory.config", "DEFAULT_EXTRACT_MODE"),
    "files.extract_min_leaf_chars": ("files_memory.config", "EXTRACT_MIN_LEAF_CHARS"),
    "files.follow_symlinks": ("files_memory.config", "FILES_FOLLOW_SYMLINKS"),
    "files.max_files_per_ingest": ("files_memory.config", "FILES_MAX_FILES_PER_INGEST"),
    "files.max_file_bytes": ("files_memory.config", "FILES_MAX_FILE_BYTES"),
    "files.max_leaf_tokens": ("files_memory.config", "FILES_MAX_LEAF_TOKENS"),
    "files.promotion_scope": ("files_memory.config", "PROMOTION_DEFAULT_SCOPE"),
    "files.prewarm_timeout_s": ("files_memory.summarize", "_PREWARM_TIMEOUT_S"),
    "files.promo_half_life_days": ("files_memory.promotability", "DEFAULT_HALF_LIFE_DAYS"),
}


def test_defaults_are_what_todays_readers_produce():
    from config_defaults_helper import mismatches

    wrong = mismatches(_PINNED)
    assert not wrong, f"registry default != today's reader (registry, today): {wrong}"


def test_settings_that_do_nothing_are_retired_not_registered():
    """Read-but-unused or always-overridden today: retired (user decision 2026-10-09)."""
    from m3_core.config import REGISTRY
    from m3_core.config.keys import RETIRED

    for env in ("M3_FILES_DEFAULT_SCOPE", "M3_FILES_EXTRACT_CONCURRENCY", "M3_FILES_EXTRACT_MAX_ATTEMPTS",
                "M3_FILES_DB_PROMPT_ON_FIRST_USE", "M3_FILES_CORPUS", "M3_FILES_DEDUP_THRESHOLD",
                "M3_FILES_DEDUP_MAX_PAIRS", "M3_FILES_DEDUP_LEAF_LIMIT", "M3_FILES_PROMO_SUGGEST_THRESHOLD"):
        assert REGISTRY.by_env(env) is None, f"{env} would be documented as a working setting"
        assert env in RETIRED, env


def test_follow_symlinks_on_is_a_changed_reading(tmp_path, caplog):
    from m3_core.config import REGISTRY, Location
    from m3_core.config import resolve as r
    from m3_core.config.settings import build

    r._LEGACY_NOTED.discard(("M3_FILES_FOLLOW_SYMLINKS", "on"))
    with caplog.at_level(logging.WARNING):
        s = build(Location(file=tmp_path / "m3_config.toml", root=tmp_path, tier="t"),
                  environ={"M3_FILES_FOLLOW_SYMLINKS": "on"})
    assert s[REGISTRY.get("files.follow_symlinks")] is True and "now reads as True" in caplog.text
