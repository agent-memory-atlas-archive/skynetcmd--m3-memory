"""Registry tables [enrich], [entity], [entity.coalesce], [gliner]."""

from __future__ import annotations

_C = "memory.config"
_EC = "files_memory.entity_coalesce"
_PINNED = {
    "enrich.profile": ("m3_enrich", "DEFAULT_PROFILE"),
    "enrich.max_attempts": ("enrichment_state", "DEFAULT_MAX_ATTEMPTS"),
    "memory.fact_enrich_concurrency": (_C, "FACT_ENRICH_CONCURRENCY"),
    "memory.fact_enrich_max_attempts": (_C, "FACT_ENRICH_MAX_ATTEMPTS"),
    "entity.extract_concurrency": (_C, "ENTITY_EXTRACT_CONCURRENCY"),
    "entity.extract_max_attempts": (_C, "ENTITY_EXTRACT_MAX_ATTEMPTS"),
    "entity.resolve_fuzzy_min": (_C, "ENTITY_RESOLVE_FUZZY_MIN"),
    "entity.resolve_cosine_min": (_C, "ENTITY_RESOLVE_COSINE_MIN"),
    "entity.seed_stoplist": (_C, "ENTITY_SEED_STOPLIST"),
    "entity.coalesce.automerge_cosine": (_EC, "AUTO_MERGE_COSINE"),
    "entity.coalesce.flag_cosine": (_EC, "FLAG_COSINE"),
    "entity.coalesce.fuzzy_high": (_EC, "FUZZY_HIGH"),
    "entity.coalesce.max_block": (_EC, "MAX_BLOCK"),
    "entity.coalesce.max_cluster": (_EC, "MAX_CLUSTER"),
}


def test_defaults_are_what_todays_readers_produce():
    from config_defaults_helper import mismatches

    wrong = mismatches(_PINNED)
    assert not wrong, f"registry default != today's reader (registry, today): {wrong}"


def test_coalesce_max_pairs_is_retired():
    """Every caller passes 1000, so the env value never takes effect."""
    from m3_core.config import REGISTRY
    from m3_core.config.keys import RETIRED

    assert REGISTRY.by_env("M3_ENTITY_COALESCE_MAX_PAIRS") is None
    assert "M3_ENTITY_COALESCE_MAX_PAIRS" in RETIRED
