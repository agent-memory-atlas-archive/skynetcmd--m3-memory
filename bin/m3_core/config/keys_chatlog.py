"""Registry tables [chatlog], [ingest], [observer], [reflector], [slm], [loop],
[autonomy], [governor], [wiki.*] and memory write-path flags (plan 2C.1;
survey 2026-10-09 over chatlog_*.py, memory/config.py, m3_cognitive_loop.py,
run_observer.py, run_reflector.py, slm_intent.py, wiki/*).

Today .spill_config.json, .cognitive_loop_config.json and .governor_config.json
win over env for their keys; they fold into [chatlog.spill], [autonomy] and
[governor] in 2D (CONFIG_DESIGN §11), which owns that precedence.
"""
from __future__ import annotations

import os

from .registry import Key, in_range, parse_bool, parse_float, parse_int


def _eq1(raw: str) -> bool:  # today's `== "1"`
    return raw == "1"


def _truthy4(raw: str) -> bool:  # today's `.strip().lower() in ("1","true","yes","on")`
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _flag(name, env, default, doc, legacy=_eq1):
    return Key[bool](name=name, parse=parse_bool, default=default, env_names=env,
                     reloadable=False, legacy_parse=legacy, doc=doc)


def _int(name, env, default, doc, lo=1, hi=None):
    return Key[int](name=name, parse=parse_int, default=default, validate=in_range(lo=lo, hi=hi),
                    env_names=env, reloadable=False, doc=doc)


def _float(name, env, default, doc):
    return Key[float](name=name, parse=parse_float, default=default, validate=in_range(lo=0),
                      env_names=env, reloadable=False, doc=doc)


def _str(name, env, default, doc):
    return Key[str](name=name, parse=str, default=default, env_names=env, reloadable=False, doc=doc)


def parse_path_list(raw: str) -> list[str]:
    """os.pathsep-separated directories (slm_intent.py:92)."""
    return [p.strip() for p in raw.split(os.pathsep) if p.strip()]


_SYNTH_URL = "http://127.0.0.1:1234/v1/chat/completions"

CHATLOG: list[Key] = [
    # ── [chatlog] ──
    _flag("chatlog.statusline", ("M3_CHATLOG_STATUSLINE", "CHATLOG_STATUSLINE"), True,
          "Show the chatlog status line.", legacy=lambda raw: raw != "off"),
    _flag("chatlog.statusline_ascii", ("M3_CHATLOG_STATUSLINE_ASCII", "CHATLOG_STATUSLINE_ASCII"), False,
          "Use [!] instead of the warning sign in the status line."),
    _int("chatlog.db.pool_size", ("M3_CHATLOG_DB_POOL_SIZE", "CHATLOG_DB_POOL_SIZE"), 4,
         "SQLite connections in the chatlog pool."),
    _int("chatlog.db.pool_timeout_s", ("M3_CHATLOG_DB_POOL_TIMEOUT", "CHATLOG_DB_POOL_TIMEOUT"), 10,
         "Seconds: the chatlog's SQLite busy timeout AND its pool checkout wait (one value, two uses today)."),
    _flag("chatlog.auto_enrich", ("M3_AUTO_ENRICH",), False,
          "Queue an observation after an ingest of chatlog.auto_enrich_min_turns or more turns.",
          legacy=_truthy4),
    _int("chatlog.auto_enrich_min_turns", ("M3_AUTO_ENRICH_MIN_TURNS",), 10,
         "Turns an ingest needs before chatlog.auto_enrich queues an observation."),
    _int("chatlog.spill.max_drain_attempts", ("M3_SPILL_MAX_ATTEMPTS",), 5,
         "Attempts to drain a spilled chatlog batch (today .spill_config.json wins)."),
    # ── [memory] write path ──
    _flag("memory.auto_related_link", ("M3_AUTO_RELATED_LINK",), True,
          "Link a new memory to related ones on write."),
    _flag("memory.auto_related_link_scope_by_variant", ("M3_AUTO_RELATED_LINK_SCOPE_BY_VARIANT",), True,
          "Only link memories within the same variant."),
    _flag("memory.inline_classify", ("M3_INLINE_CLASSIFY",), False,
          "Classify memory type inline on write instead of in the loop.", legacy=_truthy4),
    # ── [ingest] ──
    _flag("ingest.event_rows", ("M3_INGEST_EVENT_ROWS",), True, "Write event rows on chat ingest."),
    _flag("ingest.gist_rows", ("M3_INGEST_GIST_ROWS",), True, "Write gist rows on chat ingest."),
    _flag("ingest.window_chunks", ("M3_INGEST_WINDOW_CHUNKS",), True, "Write windowed chunks on chat ingest."),
    _int("ingest.window_size", ("M3_INGEST_WINDOW_SIZE",), 3, "Turns per window chunk."),
    _int("ingest.gist_min_turns", ("M3_INGEST_GIST_MIN_TURNS",), 10, "Turns a conversation needs for a gist."),
    _int("ingest.gist_stride", ("M3_INGEST_GIST_STRIDE",), 5, "Turns between gists."),
    # ── [observer] / [reflector] ──
    _str("observer.profile", ("M3_OBSERVER_PROFILE", "OBSERVER_PROFILE"), "observer_local",
         "SLM profile the observer uses."),
    _flag("observer.precise_provenance", ("M3_OBSERVER_PRECISE_PROVENANCE",), False,
          "Record turn-level provenance for observations."),
    _str("reflector.profile", ("M3_REFLECTOR_PROFILE", "REFLECTOR_PROFILE"), "reflector_local",
         "SLM profile the reflector uses."),
    # ── [slm] ──
    _flag("slm.classifier", ("M3_SLM_CLASSIFIER",), False, "Use the SLM to classify search intent.",
          legacy=lambda raw: raw.strip().lower() in ("1", "true", "yes")),
    _str("slm.profile", ("M3_SLM_PROFILE",), "default", "Default SLM profile for intent classification."),
    Key[list[str]](name="slm.profile_dirs", parse=parse_path_list, default=[],
                   env_names=("M3_SLM_PROFILES_DIR",), reloadable=False,
                   doc="Extra directories searched first for SLM profiles (os.pathsep-separated in env)."),
    # ── [loop]: the cognitive loop ──
    _int("loop.limit_per_pass", ("M3_LIMIT_PER_PASS",), 4, "Items one loop pass takes (the governor shrinks it)."),
    _float("loop.classify_deadline_s", ("M3_CLASSIFY_DEADLINE_S",), 10.0, "Seconds a classify pass may take."),
    _str("loop.distill_model", ("M3_DISTILL_MODEL",), "slm",
         "Procedure distillation model: slm, llm, or a profile name."),
    # ── [autonomy] (today .cognitive_loop_config.json wins for consolidation_auto) ──
    _flag("autonomy.consolidation_auto", ("M3_CONSOLIDATION_AUTO",), False,
          "Let the loop apply belief consolidation without review.", legacy=_truthy4),
    _flag("autonomy.distill_auto", ("M3_DISTILL_AUTO",), False,
          "Let the loop apply distilled procedures without review."),
    _flag("autonomy.chatlog_prune_auto", ("M3_CHATLOG_PRUNE_AUTO",), False,
          "Let the loop prune the chatlog without review (today .cognitive_loop_config.json wins).",
          legacy=_truthy4),
    # Read through wrappers the env gate could not see until 2026-10-09
    # (memory/enrich.py:68 _ingest_llm_enabled; run_observer.py:62 copies it).
    _flag("ingest.auto_entities", ("M3_INGEST_AUTO_ENTITIES",), False,
          "Extract entities with the LLM on ingest.", legacy=_truthy4),
    _flag("ingest.auto_title", ("M3_INGEST_AUTO_TITLE",), False,
          "Title memories with the LLM on ingest.", legacy=_truthy4),
    _flag("observer.no_embed", ("M3_OBSERVER_NO_EMBED",), False,
          "Do not embed the observations the observer writes.", legacy=_truthy4),
    # ── [governor] (today .governor_config.json wins) ──
    _int("governor.feedback_window_minutes", ("M3_FEEDBACK_WINDOW_MINUTES",), 5,
         "Minutes of feedback the maintenance pass considers.", hi=1440),
    # ── [wiki.synth] / [wiki.compile] / [wiki.drift] ──
    _str("wiki.synth.url", ("M3_WIKI_SYNTH_URL",), _SYNTH_URL, "Chat-completions URL for wiki synthesis."),
    _str("wiki.synth.model", ("M3_WIKI_SYNTH_MODEL",), None, "Synthesis model; unset uses the server's."),
    _float("wiki.synth.timeout_s", ("M3_WIKI_SYNTH_TIMEOUT",), 30.0, "Seconds a synthesis call may take."),
    _str("wiki.compile.url", ("M3_WIKI_COMPILE_URL",), None, "Prose-compile URL. Default: wiki.synth.url."),
    _str("wiki.compile.model", ("M3_WIKI_COMPILE_MODEL",), None, "Prose-compile model. Default: wiki.synth.model."),
    _float("wiki.compile.timeout_s", ("M3_WIKI_COMPILE_TIMEOUT",), 60.0, "Seconds a compile call may take."),
    _str("wiki.drift.url", ("M3_WIKI_DRIFT_URL",), None,
         "Citation-drift and derivability judge URL. Default: wiki.synth.url."),
    _str("wiki.drift.model", ("M3_WIKI_DRIFT_MODEL",), None, "Judge model. Default: wiki.synth.model."),
    _float("wiki.drift.timeout_s", ("M3_WIKI_DRIFT_TIMEOUT",), 30.0, "Seconds a judge call may take."),
]
