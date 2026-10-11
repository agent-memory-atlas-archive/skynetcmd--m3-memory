"""Registry tables [enrich], [entity], [entity.coalesce], [gliner] and the fact /
entity extraction knobs of [memory] (plan 2C.1; survey 2026-10-09 over
m3_enrich.py, enrich/prep.py, m3_entities*.py, memory/config.py,
files_memory/entity_coalesce.py).

M3_ENTITY_COALESCE_MAX_PAIRS is RETIRED (every caller passes 1000), under the
user's 2026-10-09 "retire settings that have no effect" decision.
"""

from __future__ import annotations

from .registry import Key, in_range, parse_bool, parse_float, parse_int, parse_list, parse_path, parse_str

_UNIT = in_range(lo=0, hi=1)


def _int(name, env, default, doc, lo=1, hi=None):
    return Key[int](
        name=name,
        parse=parse_int,
        default=default,
        validate=in_range(lo=lo, hi=hi),
        env_names=(env,),
        reloadable=False,
        doc=doc,
    )


def _ratio(name, env, default, doc):
    return Key[float](
        name=name, parse=parse_float, default=default, validate=_UNIT, env_names=(env,), reloadable=False, doc=doc
    )


def _path(name, env, doc):
    return Key(name=name, parse=parse_path, default=None, env_names=(env,), reloadable=False, doc=doc)


ENRICH: list[Key] = [
    # ── [enrich]: m3_enrich's run defaults (a CLI flag always wins) ──
    Key[str](
        name="enrich.profile",
        parse=parse_str,
        default="enrich_local_qwen",
        env_names=("M3_ENRICH_PROFILE",),
        reloadable=False,
        doc="SLM profile (under config/slm/) m3_enrich uses.",
    ),
    Key[bool](
        name="enrich.track_state",
        parse=parse_bool,
        default=False,
        env_names=("M3_ENRICH_TRACK_STATE",),
        reloadable=False,
        legacy_parse=lambda raw: raw.lower() in ("1", "true", "yes"),
        doc="Record per-conversation enrichment state (forced on by --resume and budgets).",
    ),
    _int("enrich.max_attempts", "M3_ENRICH_MAX_ATTEMPTS", 3, "Attempts before a conversation is dead-lettered."),
    Key[float](
        name="enrich.budget_usd",
        parse=parse_float,
        default=None,
        validate=in_range(lo=0),
        env_names=("M3_ENRICH_BUDGET_USD",),
        reloadable=False,
        doc="Spending cap for one run, in US dollars; unset is uncapped.",
    ),
    _path("enrich.conv_list", "M3_ENRICH_CONV_LIST", "File listing the conversations to enrich."),
    Key[int](
        name="enrich.input_max_k",
        parse=parse_int,
        default=None,
        validate=in_range(lo=1),
        env_names=("M3_ENRICH_INPUT_MAX_K",),
        reloadable=False,
        doc="Input cap in KiB; unset uses the profile's own.",
    ),
    Key[int](
        name="enrich.min_size_k",
        parse=parse_int,
        default=None,
        validate=in_range(lo=0),
        env_names=("M3_ENRICH_MIN_SIZE_K",),
        reloadable=False,
        doc="--resume: skip conversations smaller than this (KiB).",
    ),
    Key[int](
        name="enrich.max_size_k",
        parse=parse_int,
        default=None,
        validate=in_range(lo=0),
        env_names=("M3_ENRICH_MAX_SIZE_K",),
        reloadable=False,
        doc="--resume: skip conversations larger than this (KiB).",
    ),
    Key[str](
        name="enrich.send_to",
        parse=parse_str,
        default=None,
        env_names=("M3_ENRICH_SEND_TO",),
        reloadable=False,
        doc="--resume: only conversations assigned to this provider.",
    ),
    # ── [memory]: background fact / entity extraction ──
    _int("memory.fact_enrich_concurrency", "M3_FACT_ENRICH_CONCURRENCY", 2, "Concurrent fact extractions."),
    _int(
        "memory.fact_enrich_max_attempts",
        "M3_FACT_ENRICH_MAX_ATTEMPTS",
        3,
        "Attempts before a fact extraction is given up.",
    ),
    # ── [entity] ──
    _int("entity.extract_concurrency", "M3_ENTITY_EXTRACT_CONCURRENCY", 2, "Concurrent entity extractions."),
    _int(
        "entity.extract_max_attempts",
        "M3_ENTITY_EXTRACT_MAX_ATTEMPTS",
        3,
        "Attempts before an entity extraction is given up.",
    ),
    _ratio(
        "entity.resolve_fuzzy_min",
        "M3_ENTITY_RESOLVE_FUZZY_MIN",
        0.85,
        "Fuzzy-match score at which two entity names resolve to one entity.",
    ),
    _ratio(
        "entity.resolve_cosine_min",
        "M3_ENTITY_RESOLVE_COSINE_MIN",
        0.92,
        "Embedding similarity at which two entity names resolve to one entity.",
    ),
    Key[list[str]](
        name="entity.seed_stoplist",
        parse=parse_list,
        default=[
            "yes",
            "no",
            "ok",
            "okay",
            "thanks",
            "thank",
            "hi",
            "hello",
            "the",
            "this",
            "that",
            "there",
            "their",
            "they",
            "them",
            "it",
            "its",
        ],
        env_names=("M3_ENTITY_SEED_STOPLIST",),
        reloadable=False,
        doc="Words never used as graph seeds (case-insensitive).",
    ),
    _path(
        "entity.vocab_yaml",
        "M3_ENTITY_VOCAB_YAML",
        "Entity vocabulary. Unset: config/lists/entity_graph_default.yaml (the m3_entities CLI "
        "uses entity_graph_m3.yaml; 2D makes them one).",
    ),
    _path("entity.conv_list", "M3_ENTITIES_CONV_LIST", "File listing the conversations to extract entities from."),
    # ── [entity.coalesce]: the files-entity coalescer ──
    _ratio(
        "entity.coalesce.automerge_cosine",
        "M3_ENTITY_COALESCE_AUTOMERGE",
        0.95,
        "Similarity at which two entities merge automatically.",
    ),
    _ratio(
        "entity.coalesce.flag_cosine",
        "M3_ENTITY_COALESCE_FLAG",
        0.80,
        "Similarity at which a pair is flagged for review.",
    ),
    _int(
        "entity.coalesce.fuzzy_high",
        "M3_ENTITY_COALESCE_FUZZY_HIGH",
        90,
        "Fuzzy-name score (0-100) that counts as a strong match.",
        lo=0,
        hi=100,
    ),
    _int("entity.coalesce.max_block", "M3_ENTITY_COALESCE_MAX_BLOCK", 400, "Entities compared in one block."),
    _int(
        "entity.coalesce.max_cluster",
        "M3_ENTITY_COALESCE_MAX_CLUSTER",
        12,
        "Largest same_as cluster an automatic merge may build.",
    ),
    # ── [gliner]: m3_entities_gliner (today env OVERRIDES its CLI flags; 2D fixes) ──
    Key[str](
        name="gliner.model",
        parse=parse_str,
        default="urchade/gliner_large-v2.1",
        env_names=("M3_GLINER_MODEL",),
        reloadable=False,
        doc="GLiNER model (Hugging Face id).",
    ),
    _ratio("gliner.threshold", "M3_GLINER_THRESHOLD", 0.5, "Entity confidence threshold."),
    _int("gliner.batch_size", "M3_GLINER_BATCH_SIZE", 32, "Texts per GLiNER batch."),
    Key[str](
        name="gliner.device",
        parse=parse_str,
        default="cuda",
        env_names=("M3_GLINER_DEVICE",),
        reloadable=False,
        doc="Device: cuda (falls back to mps, then cpu), cpu, or a torch device.",
    ),
]
