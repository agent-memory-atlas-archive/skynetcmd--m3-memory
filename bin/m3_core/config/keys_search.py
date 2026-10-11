"""Registry tables [search], [memory.*] (plan 2C.1; survey 2026-10-09 over
bin/memory/config.py, search.py, search_routing.py, search_ranking.py, entity.py).

Held on the ratchet for the user (read but unused, or parsed several ways):
M3_CONFIDENCE_MODEL, M3_TRUST_AUTOTUNE, M3_ENABLE_ENTITY_GRAPH,
M3_ROUTE_SHADOW_MODE. M3_LIMIT_PER_PASS belongs to the cognitive loop's table.
"""

from __future__ import annotations

from .registry import Key, in_range, one_of, parse_bool, parse_float, parse_int, parse_list, parse_str

_NONNEG = in_range(lo=0)


def _eq1(raw: str) -> bool:  # today's `== "1"`: "true" and "on" read as off
    return raw == "1"


def _int(name, env, default, doc, lo=1):
    return Key[int](
        name=name, parse=parse_int, default=default, validate=in_range(lo=lo), env_names=env, reloadable=False, doc=doc
    )


def _float(name, env, default, doc):
    return Key[float](
        name=name, parse=parse_float, default=default, validate=_NONNEG, env_names=env, reloadable=False, doc=doc
    )


def _flag(name, env, default, doc, legacy=_eq1):
    return Key[bool](
        name=name, parse=parse_bool, default=default, env_names=env, reloadable=False, legacy_parse=legacy, doc=doc
    )


def _lower(raw: str) -> str:
    return raw.strip().lower()


SEARCH: list[Key] = [
    # ── [search] ──
    _int("search.row_cap", ("M3_SEARCH_ROW_CAP", "SEARCH_ROW_CAP"), 5000, "Candidate rows a search reads."),
    _int(
        "search.short_turn_threshold",
        ("M3_SHORT_TURN_THRESHOLD", "SHORT_TURN_THRESHOLD"),
        20,
        "Turns shorter than this (characters) are down-weighted.",
        lo=0,
    ),
    _float(
        "search.title_match_boost",
        ("M3_TITLE_MATCH_BOOST", "TITLE_MATCH_BOOST"),
        0.15,
        "Score added when the query matches the title.",
    ),
    _float(
        "search.importance_weight",
        ("M3_IMPORTANCE_WEIGHT", "IMPORTANCE_WEIGHT"),
        0.15,
        "Weight of a memory's importance in ranking.",
    ),
    _float(
        "search.supersedes_penalty",
        ("M3_SUPERSEDES_PENALTY", "SUPERSEDES_PENALTY"),
        0.5,
        "Multiplier for a superseded memory's score (1.0 = no penalty).",
    ),
    _flag(
        "search.speaker_in_title",
        ("M3_SPEAKER_IN_TITLE", "SPEAKER_IN_TITLE"),
        True,
        "Index the speaker in chat-turn titles.",
    ),
    _flag("search.confidence_ranking", ("M3_CONFIDENCE_RANKING",), False, "Blend memory confidence into the score."),
    _float(
        "search.confidence_weight",
        ("M3_CONFIDENCE_WEIGHT",),
        0.10,
        "Weight of confidence when search.confidence_ranking is on.",
    ),
    _int("search.elbow_min_input", ("M3_ELBOW_MIN_INPUT",), 20, "Fewest results before elbow truncation applies."),
    _int("search.elbow_min_return", ("M3_ELBOW_MIN_RETURN",), 8, "Fewest results elbow truncation keeps."),
    _float("search.elbow_abs_threshold", ("M3_ELBOW_ABS_THRESHOLD",), 0.05, "Score drop that counts as the elbow."),
    _float(
        "search.expansion_displacement_margin",
        ("M3_EXPANSION_DISPLACEMENT_MARGIN",),
        2.0,
        "How far an expanded result must outscore one it displaces.",
    ),
    _int(
        "search.expansion_protected_ranks",
        ("M3_EXPANSION_PROTECTED_RANKS",),
        3,
        "Top ranks query expansion may not displace.",
        lo=0,
    ),
    _flag("search.query_type_routing", ("M3_QUERY_TYPE_ROUTING",), True, "Route searches by query type."),
    _flag("search.intent_routing", ("M3_INTENT_ROUTING",), True, "Route searches by intent."),
    _float(
        "search.intent_user_fact_boost",
        ("M3_INTENT_USER_FACT_BOOST",),
        0.20,
        "Boost for user facts on a user-fact intent.",
    ),
    _float(
        "search.intent_procedural_boost",
        ("M3_INTENT_PROCEDURAL_BOOST",),
        0.20,
        "Boost for procedures on a procedural intent.",
    ),
    Key[str](
        name="search.rerank_model",
        parse=parse_str,
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        env_names=("M3_RERANK_MODEL",),
        reloadable=False,
        doc="Cross-encoder used to rerank.",
    ),
    _int(
        "search.observation_budget_tokens",
        ("M3_OBSERVATION_BUDGET_TOKENS",),
        4000,
        "Tokens of observations a two-stage search returns.",
    ),
    _float(
        "search.two_stage_turn_penalty",
        ("M3_TWO_STAGE_TURN_PENALTY",),
        0.7,
        "Multiplier for raw turns in a two-stage search.",
    ),
    _int(
        "search.two_stage_max_turns_per_observation",
        ("M3_TWO_STAGE_MAX_TURNS_PER_OBS",),
        3,
        "Supporting turns kept per observation.",
    ),
    _int(
        "search.router_temporal_k_bump",
        ("M3_ROUTER_TEMPORAL_K_BUMP",),
        5,
        "Extra results a temporal query fetches.",
        lo=0,
    ),
    Key[int](
        name="search.bypass_surface_cap",
        parse=parse_int,
        default=300,
        validate=in_range(lo=1, hi=5000),
        env_names=("M3_BYPASS_SURFACE_CAP",),
        reloadable=False,
        doc="Entity-bypass results surfaced (1-5000; today a larger value is clamped).",
    ),
    # ── [memory.dedup] / [memory.contradiction] / [memory.corroboration] ──
    _int("memory.dedup.limit", ("M3_DEDUP_LIMIT", "DEDUP_LIMIT"), 1000, "Memories one dedup pass compares."),
    _float(
        "memory.dedup.threshold",
        ("M3_DEDUP_THRESHOLD", "DEDUP_THRESHOLD"),
        0.92,
        "Similarity above which two memories are duplicates.",
    ),
    _float(
        "memory.contradiction.threshold",
        ("M3_CONTRADICTION_THRESHOLD", "CONTRADICTION_THRESHOLD"),
        0.92,
        "Similarity above which a write is checked for contradiction.",
    ),
    Key[str](
        name="memory.contradiction.title_gate",
        parse=_lower,
        default="loose",
        validate=one_of("strict", "loose", "off"),
        env_names=("M3_CONTRADICTION_TITLE_GATE", "CONTRADICTION_TITLE_GATE"),
        reloadable=False,
        doc="Title agreement a contradiction needs: strict, loose or off. "
        "(Today any other value acts as off; it is now an error.)",
    ),
    Key[list[str]](
        name="memory.contradiction.type_exclusions",
        parse=parse_list,
        default=["conversation"],
        env_names=("M3_CONTRADICTION_TYPE_EXCLUSIONS", "CONTRADICTION_TYPE_EXCLUSIONS"),
        reloadable=False,
        doc="Memory types never checked for contradiction.",
    ),
    _flag(
        "memory.corroboration.enabled",
        ("M3_CORROBORATION",),
        False,
        "Record corroborating writes instead of storing duplicates.",
    ),
    _float(
        "memory.corroboration.threshold",
        ("M3_CORROBORATION_THRESHOLD", "CORROBORATION_THRESHOLD"),
        0.95,
        "Similarity above which a write corroborates an existing memory.",
    ),
    # Phase L gates: on when set, else switched on automatically once enough
    # observations exist (memory.disable_auto_activation stops that). They were read
    # by a dynamic env name in memory/db.py `_gate_active`, outside the registry.
    _flag(
        "search.prefer_observations",
        ("M3_PREFER_OBSERVATIONS",),
        False,
        "Rank distilled observations ahead of raw turns (also on automatically once 100 exist).",
        legacy=lambda raw: raw.strip().lower() in ("1", "true", "yes", "on"),
    ),
    _flag(
        "search.two_stage_observations",
        ("M3_TWO_STAGE_OBSERVATIONS",),
        False,
        "Expand observation hits to their source turns (also on automatically once 100 observations exist).",
        legacy=lambda raw: raw.strip().lower() in ("1", "true", "yes", "on"),
    ),
    # ── [memory] ──
    _flag(
        "memory.fact_enriched",
        ("M3_ENABLE_FACT_ENRICHED",),
        True,
        "Enrich writes with extracted facts.",
        legacy=lambda raw: raw.lower() in ("1", "true", "yes"),
    ),
    # User decision 2026-10-09: one key, canonical parse. Today three readers
    # disagree (memory/config.py:332, entity.py:242, db.py:652 accepts "on");
    # 2D makes them one and proves or removes the count gate.
    _flag(
        "memory.entity_graph",
        ("M3_ENABLE_ENTITY_GRAPH",),
        True,
        "Build the entity graph on write.",
        legacy=lambda raw: raw.lower() in ("1", "true", "yes"),
    ),
]
