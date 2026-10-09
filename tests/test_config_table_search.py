"""Registry tables [search], [memory.*] (plan 2C.1). Defaults pinned to today's constants."""
from __future__ import annotations

_C = "memory.config"
_PINNED = {
    "search.row_cap": (_C, "SEARCH_ROW_CAP"),
    "search.short_turn_threshold": (_C, "SHORT_TURN_THRESHOLD"),
    "search.title_match_boost": (_C, "TITLE_MATCH_BOOST"),
    "search.importance_weight": (_C, "IMPORTANCE_WEIGHT"),
    "search.supersedes_penalty": (_C, "SUPERSEDES_PENALTY"),
    "search.speaker_in_title": (_C, "SPEAKER_IN_TITLE"),
    "search.confidence_ranking": (_C, "CONFIDENCE_RANKING"),
    "search.confidence_weight": (_C, "CONFIDENCE_WEIGHT"),
    "search.elbow_min_input": (_C, "ELBOW_MIN_INPUT"),
    "search.elbow_min_return": (_C, "ELBOW_MIN_RETURN"),
    "search.elbow_abs_threshold": (_C, "ELBOW_ABS_THRESHOLD"),
    "search.expansion_displacement_margin": (_C, "EXPANSION_DISPLACEMENT_MARGIN"),
    "search.expansion_protected_ranks": (_C, "EXPANSION_PROTECTED_RANKS"),
    "search.query_type_routing": (_C, "QUERY_TYPE_ROUTING"),
    "search.intent_routing": (_C, "INTENT_ROUTING"),
    "search.intent_user_fact_boost": (_C, "INTENT_USER_FACT_BOOST"),
    "search.intent_procedural_boost": (_C, "INTENT_PROCEDURAL_BOOST"),
    "search.rerank_model": (_C, "DEFAULT_RERANK_MODEL"),
    "search.bypass_surface_cap": ("memory.entity", "_BYPASS_SURFACE_CAP_DEFAULT"),
    "memory.dedup.limit": (_C, "DEDUP_LIMIT"),
    "memory.dedup.threshold": (_C, "DEDUP_THRESHOLD"),
    "memory.contradiction.threshold": (_C, "CONTRADICTION_THRESHOLD"),
    "memory.contradiction.title_gate": (_C, "CONTRADICTION_TITLE_GATE"),
    "memory.contradiction.type_exclusions": (_C, "CONTRADICTION_TYPE_EXCLUSIONS"),
    "memory.corroboration.enabled": (_C, "CORROBORATION"),
    "memory.corroboration.threshold": (_C, "CORROBORATION_THRESHOLD"),
    "memory.fact_enriched": (_C, "ENABLE_FACT_ENRICHED"),
    "memory.entity_graph": (_C, "ENABLE_ENTITY_GRAPH"),
}


def test_defaults_are_what_todays_readers_produce():
    from config_defaults_helper import mismatches

    wrong = mismatches(_PINNED)
    assert not wrong, f"registry default != today's reader (registry, today): {wrong}"


def test_decided_names_land_where_the_user_put_them():
    """User decisions 2026-10-09: two retired, one dev plumbing, one registered."""
    from m3_core.config import REGISTRY
    from m3_core.config.keys import NOT_SETTINGS, RETIRED

    assert {"M3_CONFIDENCE_MODEL", "M3_TRUST_AUTOTUNE"} <= set(RETIRED)
    assert "M3_ROUTE_SHADOW_MODE" in NOT_SETTINGS
    assert REGISTRY.by_env("M3_ENABLE_ENTITY_GRAPH").name == "memory.entity_graph"


def test_an_unknown_title_gate_is_an_error_not_off(tmp_path):
    import pytest
    from m3_core.config import Location, format_config_error
    from m3_core.config.resolve import ConfigEnvErrors
    from m3_core.config.settings import build

    with pytest.raises(ConfigEnvErrors) as exc:
        build(Location(file=tmp_path / "m3_config.toml", root=tmp_path, tier="t"),
              environ={"M3_CONTRADICTION_TITLE_GATE": "lose"})
    assert "not one of strict, loose, off" in format_config_error(exc.value)
