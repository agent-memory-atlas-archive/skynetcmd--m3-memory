"""Registry table [files] (plan 2C.1; survey 2026-10-09 over bin/files_memory/).

Only knobs that TAKE EFFECT today are registered. Read-but-unused or
always-overridden names (M3_FILES_DEFAULT_SCOPE, _EXTRACT_CONCURRENCY,
_EXTRACT_MAX_ATTEMPTS, _DB_PROMPT_ON_FIRST_USE, _CORPUS, _DEDUP_*,
_PROMO_SUGGEST_THRESHOLD) stay on the ratchet until the user decides to wire
or retire each: registering them would publish a setting that does nothing
(DESIGN_PHILOSOPHIES §3). M3_FILES_DB_PATH is a store location (2C.3).
"""

from __future__ import annotations

from .registry import Key, in_range, one_of, parse_bool, parse_float, parse_int


def _int(name: str, env: str, default: int, doc: str, lo: int = 1) -> Key[int]:
    return Key[int](
        name=name,
        parse=parse_int,
        default=default,
        validate=in_range(lo=lo),
        env_names=(env,),
        reloadable=False,
        doc=doc,
    )


def _str(name: str, env: str, default: str | None, doc: str) -> Key[str]:
    return Key[str](name=name, parse=str, default=default, env_names=(env,), reloadable=False, doc=doc)


FILES: list[Key] = [
    _str(
        "files.default_corpus",
        "M3_FILES_DEFAULT_CORPUS",
        "default",
        "Corpus a file is ingested into when none is named.",
    ),
    Key[str](
        name="files.default_extract_mode",
        parse=str,
        default="none",
        validate=one_of("none", "inline", "queue"),
        env_names=("M3_FILES_DEFAULT_EXTRACT_MODE",),
        reloadable=False,
        doc="Fact extraction on ingest: none, inline, or queued for the loop.",
    ),
    _int(
        "files.extract_min_leaf_chars",
        "M3_FILES_EXTRACT_MIN_LEAF_CHARS",
        120,
        "Leaves shorter than this are not sent to extraction.",
        lo=0,
    ),
    _str(
        "files.extract_model",
        "M3_FILES_EXTRACT_MODEL",
        None,
        "Extraction model. Default: files.summary_model, then the endpoint's discovered model.",
    ),
    _str(
        "files.extract_url",
        "M3_FILES_EXTRACT_URL",
        None,
        "Extraction endpoint. Default: files.summary_url, then M3_LMSTUDIO_URL, then the failover list.",
    ),
    _str(
        "files.summary_model",
        "M3_FILES_SUMMARY_MODEL",
        None,
        "Summary model. Default: the endpoint's discovered model.",
    ),
    _str(
        "files.summary_url",
        "M3_FILES_SUMMARY_URL",
        None,
        "Summary endpoint. Default: M3_LMSTUDIO_URL; unset disables summaries.",
    ),
    Key[bool](
        name="files.follow_symlinks",
        parse=parse_bool,
        default=False,
        env_names=("M3_FILES_FOLLOW_SYMLINKS",),
        reloadable=False,
        legacy_parse=lambda raw: raw.lower() in ("1", "true", "yes"),
        doc="Follow symbolic links while walking a directory.",
    ),
    _int("files.max_files_per_ingest", "M3_FILES_MAX_FILES_PER_INGEST", 10000, "Files one ingest call may take."),
    _int("files.max_file_bytes", "M3_FILES_MAX_FILE_BYTES", 10 * 1024 * 1024, "Larger files are skipped."),
    _int("files.max_leaf_tokens", "M3_FILES_MAX_LEAF_TOKENS", 7000, "Tokens per chunk leaf."),
    Key[float](
        name="files.prewarm_timeout_s",
        parse=parse_float,
        default=180.0,
        validate=in_range(lo=0),
        env_names=("M3_FILES_PREWARM_TIMEOUT_S",),
        reloadable=False,
        doc="Seconds to wait for the summary model to load before an ingest.",
    ),
    _str("files.promotion_scope", "M3_FILES_PROMOTION_SCOPE", "user", "Scope given to memories promoted from files."),
    Key[float](
        name="files.promo_half_life_days",
        parse=parse_float,
        default=30.0,
        validate=in_range(lo=0),
        env_names=("M3_FILES_PROMO_HALF_LIFE_DAYS",),
        reloadable=False,
        doc="Half-life of the promotability score's recency term (values below 1 act as 1).",
    ),
]
