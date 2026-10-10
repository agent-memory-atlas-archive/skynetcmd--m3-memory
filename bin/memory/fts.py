"""FTS5 query helpers and title-overlap math.

Pulled out of legacy `bin/memory_core.py` in Phase 2. Pure functions with
no I/O. The regexes and translation tables are module-level constants set
at import; everything else is stateless.

Used by the search and write paths. Re-exported through `memory_core` for
back-compat with the 22+ callers that import these directly.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

__all__ = [
    "_DATE_MONTHS",
    "_DATE_RE_ISO",
    "_DATE_RE_LONG",
    "_ENTITY_MENTION_PATTERNS",
    "_ENTITY_MENTION_RE",
    "_EVENT_DATE_HINT",
    "_EVENT_PROPER_NOUN",
    "_EVENT_SENT_SPLIT",
    "_EVENT_VERB_LIST",
    "_EVENT_VERB_RE",
    "_TEMPORAL_QUERY_RE",
    "_TEMPORAL_ROUTER_PATTERNS",
    "_TEMPORAL_ROUTER_RE",
    "_augment_title_with_role",
    "_compile_fts_query",
    "_compile_tsquery",
    "_query_title_overlap",
    "_query_title_token_set",
    "_sanitize_for_searchable",
    "_sanitize_fts",
    "_title_overlap_from_qset",
]
import logging

logger = logging.getLogger("memory.fts")


from . import config

# ──────────────────────────────────────────────────────────────────────────────
# Temporal & Entity patterns (hoisted from search.py to break cycles)
# ──────────────────────────────────────────────────────────────────────────────
_DATE_RE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_RE_LONG = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
    r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)(?:\s+,?\s*(\d{4}))?\b",
    re.IGNORECASE,
)
_DATE_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_TEMPORAL_QUERY_RE = re.compile(
    r"\b(?:last|next|past|this|current|previous)\b|\b(?:today|yesterday|tomorrow)\b|"
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b|"
    r"\d{4}-\d{2}-\d{2}|"  # ISO date
    r"\b(?:morning|afternoon|evening|night|week|month|year|ago|recently|lately)\b",
    re.IGNORECASE,
)

# Module-level temporal regex — same patterns memory `2d1d5812` documented;
# 100% recall on LongMemEval temporal-reasoning, low FPR on others.
# (Restored from pre-Phase-7+8 search.py; the refactor lost ~10 patterns
# when consolidating into fts.py — see test_memory_search_routed.py.)
_TEMPORAL_ROUTER_PATTERNS = (
    r"\bwhen\b",
    r"\bhow long\b",
    r"\bwhat\s+(?:date|day|month|year|time)\b",
    r"\bbefore\b",
    r"\bafter\b",
    r"\bsince\b",
    r"\buntil\b",
    r"\b(?:days?|weeks?|months?|years?)\s+ago\b",
    r"\bfirst\b",
    r"\blast\b",
    r"\brecent(?:ly)?\b",
    r"\bearliest\b",
    r"\blatest\b",
    r"\bwhich\s+\w+\s+first\b",
    r"\bin\s+what\s+order\b",
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b",
    r"\bvalentine'?s?\s+day\b",
    r"\bchristmas\b",
    r"\bthanksgiving\b",
    r"\bnew\s+year'?s?\b",
)
_TEMPORAL_ROUTER_RE = re.compile("|".join(_TEMPORAL_ROUTER_PATTERNS), re.IGNORECASE)

_ENTITY_MENTION_PATTERNS = (
    r'"[^"]+"',  # double-quoted strings
    r"'[^']+'",  # single-quoted strings
    r"\b(?:19|20)\d{2}\b",  # 4-digit years (1900–2099)
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2}\b",  # Month Day
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*",  # Capitalized noun phrases
)
_ENTITY_MENTION_RE = re.compile("|".join(_ENTITY_MENTION_PATTERNS))

# Event extraction patterns
_EVENT_VERB_LIST = (
    "decided",
    "started",
    "finished",
    "completed",
    "installed",
    "configured",
    "updated",
    "removed",
    "fixed",
    "bought",
    "purchased",
    "booked",
    "traveled",
    "met",
    "attended",
    "presented",
    "wrote",
    "released",
    "launched",
    "deployed",
)
_EVENT_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_EVENT_DATE_HINT = re.compile(
    r"\b(?:today|yesterday|tomorrow|last|next|this|on|at|in|ago|from|to)\b|\d{4}-\d{2}-\d{2}", re.IGNORECASE
)
_EVENT_VERB_RE = re.compile(r"\b(" + "|".join(_EVENT_VERB_LIST) + r")\b", re.IGNORECASE)
_EVENT_PROPER_NOUN = re.compile(r"\b([A-Z][a-z]{2,})\b")


# Keyword-query sanitizers and compilers live in the database seam
# (memory/backends/keyword.py); re-exported for existing importers. Explicit
# `as` form: a ruff F401 fix must not strip them.
from .backends.keyword import _FTS_NON_TERM as _FTS_NON_TERM  # noqa: E402
from .backends.keyword import _FTS_OPERATORS as _FTS_OPERATORS  # noqa: E402
from .backends.keyword import _SEARCHABLE_PUNCT as _SEARCHABLE_PUNCT  # noqa: E402
from .backends.keyword import _compile_fts_query as _compile_fts_query  # noqa: E402
from .backends.keyword import _compile_fts_query_py as _compile_fts_query_py  # noqa: E402
from .backends.keyword import _compile_tsquery as _compile_tsquery  # noqa: E402
from .backends.keyword import _sanitize_for_searchable as _sanitize_for_searchable  # noqa: E402
from .backends.keyword import _sanitize_fts as _sanitize_fts  # noqa: E402
from .backends.keyword import _sanitize_fts_py as _sanitize_fts_py  # noqa: E402

_TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def _augment_title_with_role(title: str, metadata: str | dict | None) -> str:
    """Prepend '[role] ' to title when metadata carries a person-name role.

    Makes the speaker visible to FTS so queries like 'what did Caroline say
    about X' can match turns by Caroline. Idempotent: skips when title is
    already bracket-prefixed. Gated by config.SPEAKER_IN_TITLE.
    """
    if not config.SPEAKER_IN_TITLE:
        return title or ""
    t = (title or "").strip()
    if t.startswith("["):
        return t
    if not metadata:
        return t
    try:
        meta = metadata if isinstance(metadata, dict) else json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        return t
    role = (meta.get("role") or "").strip()
    # Only prepend when role looks like a proper name (avoid 'user'/'assistant'
    # generics which add noise without helping real-world queries).
    if not role or role.lower() in ("user", "assistant", "system", "tool"):
        return t
    return f"[{role}] {t}".strip()


@lru_cache(maxsize=1024)
def _query_title_token_set(query: str) -> frozenset[str]:
    """Tokenize a query into the set used for title-overlap scoring.

    Hoisted out of `_query_title_overlap` so callers in a hot loop can
    compute it once and reuse it across many titles. Returns frozenset
    for safe sharing.

    lru-cached at 1024 entries: searches frequently repeat the same query
    (bench harnesses, redo-typed-query flows, paginated search), and
    tokenization is pure-Python regex+split work — cache hit avoids it
    entirely. Frozenset return makes the cached value safe to share
    across concurrent callers.
    """
    if not query:
        return frozenset()
    return frozenset(t for t in _TOKEN_SPLIT.split(query.lower()) if len(t) > 2)


def _title_overlap_from_qset(q_tokens: frozenset[str], title: str) -> float:
    """Same as `_query_title_overlap` but with the query token set precomputed."""
    if not q_tokens or not title:
        return 0.0
    t_tokens = {t for t in _TOKEN_SPLIT.split(title.lower()) if len(t) > 2}
    if not t_tokens:
        return 0.0
    overlap = q_tokens & t_tokens
    return len(overlap) / len(q_tokens) if q_tokens else 0.0


def _query_title_overlap(query: str, title: str) -> float:
    """Fraction of query tokens that also appear in title. 0.0 when no overlap.

    Used as a small ranker boost for titles that literally echo query terms.
    Kept for back-compat with single-call callers; hot loops should use
    `_query_title_token_set` once + `_title_overlap_from_qset` per title.
    """
    if not query or not title:
        return 0.0
    return _title_overlap_from_qset(_query_title_token_set(query), title)
