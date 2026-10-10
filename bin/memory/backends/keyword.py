"""Keyword-query compilation: the database seam's owner of FTS5 and tsquery syntax.

A raw user query becomes the backend's match syntax here: SQLite FTS5 MATCH
(`_compile_fts_query`) or a PostgreSQL tsquery (`_compile_tsquery`), both through
the SAME sanitization (`_sanitize_fts` + `_sanitize_for_searchable`), so query
terms normalize identically on every backend. Each Dialect exposes its compiler
as `compile_keyword_query(query, mode)`; call sites ask the dialect.

Moved verbatim from memory/fts.py (2026-10-10), which re-exports these names for
existing importers. The SQLite path routes through m3_core_rs when the native
core provides it; tests/test_fts_parity.py holds the pure-Python bodies to the
Rust port byte for byte.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import cast

from m3_core.errors import log_swallowed

from .. import config

_log = logging.getLogger("memory.backends.keyword")

# ──────────────────────────────────────────────────────────────────────────────
# FTS5 sanitization
# ──────────────────────────────────────────────────────────────────────────────
# Neutralizes FTS5 query syntax in user input so an arbitrary phrase can never
# turn into a parse error or query injection. Two passes:
#
#   1. Operator WORDS (OR/AND/NOT/NEAR) — dangerous even though alphanumeric, so
#      a punctuation strip alone won't catch them. Matched as whole words.
#   2. Every non-(word|whitespace) CHARACTER → space. This is an ALLOWLIST, not
#      a blocklist: in an FTS5 MATCH a *bare* term, almost all punctuation is a
#      syntax error or operator — `-`/`:`/`{` silently change the parse
#      ("gpt-4o" -> column-negation -> `no such column: 4o`), while `/ ! . , ; @
#      = < > ~ | # $ % & \` ( ) [ ] *` raise outright. A whitelist regex that
#      enumerated "bad" chars repeatedly missed cases (`-`, then `:`, then `/`)
#      and shipped a content-dependent ("intermittent") crash: any search with a
#      model name / hyphenated id / range / field:value threw, while plain-word
#      searches worked. Replacing with a space (not deleting) aligns the query
#      with the default tokenizer, which already splits indexed content on these
#      same characters — so `gpt-4o` -> `gpt 4o` still matches the stored row.
#
# Quoted exact-phrase queries are handled by `_compile_fts_query` BEFORE this
# function is reached, so stripping quotes here is safe for that path.
# `_FTS_OPERATORS` keeps its name (re-exported through the memory_core shim and
# referenced by tests/test_fts_parity.py) but now matches operator WORDS only;
# punctuation is handled by the broader `_FTS_NON_TERM` allowlist below.
_FTS_OPERATORS = re.compile(r"\b(OR|AND|NOT|NEAR)\b")
_FTS_NON_TERM = re.compile(r"[^\w\s]", re.UNICODE)


def _sanitize_fts(query: str, max_len: int = 500) -> str:
    """Neutralize FTS5 query syntax in user input to prevent injection / crashes.

    Returns a string safe to drop into an FTS5 `MATCH` as bare terms: operator
    words removed, all non-word/non-space characters replaced with spaces.

    Oxidation: routed through m3_core_rs.sanitize_fts (byte-exact Rust port,
    no regex engine) when the extension exposes it; the regex body below is the
    parity fallback. Parity gated by tests/test_fts_parity.py. Note the Rust
    port MUST mirror the allowlist below — see crates/m3-fts/src/lib.rs.
    """
    _rs = config.m3_core_rs
    # hasattr guard: older installed wheels predate the FTS bindings (the
    # function is simply absent). Probe explicitly instead of catching the
    # AttributeError — a bare except here is what silently masked a stale wheel
    # falling back to Python for days (see DESIGN §3, fail loud not silent).
    if _rs is not None and hasattr(_rs, "sanitize_fts"):
        try:
            return cast("str", _rs.sanitize_fts(query, max_len))
        except Exception as e:  # noqa: BLE001 — an FFI hiccup falls back to Python
            log_swallowed(_log, e, "m3_core_rs.sanitize_fts failed; using Python: %s", e)
    return _sanitize_fts_py(query, max_len)


def _sanitize_fts_py(query: str, max_len: int = 500) -> str:
    """The pure-Python body of :func:`_sanitize_fts`: the reference the Rust port
    is held to (tests/test_fts_parity.py calls THIS, never a copy)."""
    if len(query) > max_len:
        query = query[:max_len]
    query = _FTS_OPERATORS.sub(" ", query)
    query = _FTS_NON_TERM.sub(" ", query)
    return query.strip()


# Mirror of the SQLite mi_fts_insert trigger sanitization. The trigger
# lowercases and replaces these 8 punctuation chars with spaces before storing
# in content_searchable / title_searchable. Query-side text must apply the same
# transform so MATCH terms align with what FTS5 indexed.
_SEARCHABLE_PUNCT = str.maketrans(dict.fromkeys("?!:.,;/\"'", " "))


def _sanitize_for_searchable(text: str) -> str:
    """Apply the same lowercase + depunctuate transform as the FTS triggers."""
    if not text:
        return ""
    return text.lower().translate(_SEARCHABLE_PUNCT)


# Cached compilation of (raw_query, mode) -> (fts_string, ok). Hot path: search
# is called repeatedly with similar queries inside a session, and the cache
# absorbs the regex + splitting cost.
@lru_cache(maxsize=2048)
def _compile_fts_query(query: str, mode: str) -> tuple[str, bool]:
    """Compile a raw user query into an FTS5 MATCH string.

    Returns ``(fts_query, ok)``. When ``ok`` is False the caller should treat
    this as "no matchable tokens"; in ``mode == "fts5"`` that means return no
    results, in any other mode that means fall back to semantic-only.

    Exact-mode preserves the quoted phrase as-is; otherwise the query is
    depunctuated to match the FTS trigger's normalized storage, then either
    wildcarded (single-token alnum) or OR-joined (multi-token in ``fts5`` mode)
    or passed straight through.

    Oxidation: routed through m3_core_rs.compile_fts_query (byte-exact Rust port)
    when the extension is present; the Python body below is the parity fallback.
    The @lru_cache wraps this function, so the FFI crossing is paid once per
    unique (query, mode). Parity gated by tests/test_fts_parity.py.
    """
    _rs = config.m3_core_rs
    # hasattr guard mirrors _sanitize_fts: older wheels lack compile_fts_query,
    # and a bare except would silently mask the stale-wheel fallback (DESIGN §3).
    if _rs is not None and hasattr(_rs, "compile_fts_query"):
        try:
            return tuple(_rs.compile_fts_query(query, mode))
        except Exception as e:  # noqa: BLE001 — an FFI hiccup falls back to Python
            log_swallowed(_log, e, "m3_core_rs.compile_fts_query failed; using Python: %s", e)
    return _compile_fts_query_py(query, mode)


def _compile_fts_query_py(query: str, mode: str) -> tuple[str, bool]:
    """The pure-Python body of :func:`_compile_fts_query`: the reference the Rust
    port is held to (tests/test_fts_parity.py calls THIS, never a copy). Uses the
    pure-Python sanitizer, so the whole reference path is Python."""
    is_exact_query = (query.startswith('"') and query.endswith('"')) or (query.startswith("'") and query.endswith("'"))
    if is_exact_query:
        # Escape interior double-quotes by doubling them — FTS5's own escape
        # rule. Without this, an input like `"alpha"beta"` re-wraps to the
        # unterminated string `"alpha"beta"` and FTS5 MATCH raises.
        inner = query[1:-1].replace('"', '""')
        return f'"{inner}"', True
    clean = _sanitize_fts_py(query)
    clean = _sanitize_for_searchable(clean)
    if not clean.strip():
        return "", False
    clean = clean.strip()
    if mode == "fts5":
        toks = [t for t in clean.split() if t]
        if len(toks) > 1:
            return " OR ".join(toks), True
        return (f"{clean}*" if clean.isalnum() else clean), True
    # hybrid / semantic fallback path
    if " " not in clean and clean.isalnum():
        return f"{clean}*", True
    return clean, True


@lru_cache(maxsize=2048)
def _compile_tsquery(query: str, mode: str) -> tuple[str, bool]:
    """Compile a raw user query into a PostgreSQL ``tsquery`` string.

    The Postgres analogue of :func:`_compile_fts_query`. Returns ``(tsquery, ok)``
    with the same ``ok`` contract (False = no matchable tokens). It reuses the
    IDENTICAL sanitization pipeline (``_sanitize_fts`` + ``_sanitize_for_searchable``)
    so query terms normalize the same way on both backends — only the final
    assembly into match syntax differs:

      * FTS5 multi-token (fts5 mode) -> ``a OR b``;  tsquery -> ``a | b``
      * single alnum token wildcard  ``foo*``    ;  tsquery -> ``foo:*``
      * exact quoted phrase ``"a b"``            ;  tsquery -> ``a <-> b`` (phrase)

    The result is meant to be passed to ``to_tsquery('english', <result>)``.
    Terms are already sanitized to bare word characters, so they are safe to
    interpolate — but the caller should still bind via a parameter and let
    ``to_tsquery`` parse it. Ordering/scoring is NOT comparable to bm25 (§8.2).
    """
    is_exact_query = (query.startswith('"') and query.endswith('"')) or (query.startswith("'") and query.endswith("'"))
    if is_exact_query:
        inner = _sanitize_for_searchable(_sanitize_fts(query[1:-1]))
        toks = [t for t in inner.split() if t]
        if not toks:
            return "", False
        # phrase match: consecutive lexemes via the <-> (FOLLOWED BY) operator
        return " <-> ".join(toks), True
    clean = _sanitize_for_searchable(_sanitize_fts(query)).strip()
    if not clean:
        return "", False
    toks = [t for t in clean.split() if t]
    if not toks:
        return "", False
    if mode == "fts5":
        if len(toks) > 1:
            return " | ".join(toks), True  # OR, mirroring fts5-mode OR-join
        tok = toks[0]
        return (f"{tok}:*" if tok.isalnum() else tok), True
    # hybrid / semantic fallback: single alnum token gets a prefix wildcard
    if len(toks) == 1 and toks[0].isalnum():
        return f"{toks[0]}:*", True
    # Multi-token: AND (`&`), mirroring FTS5's IMPLICIT AND for an unquoted
    # multi-token query. _compile_fts_query's hybrid branch returns the bare
    # `clean` string, and FTS5 treats space-separated tokens as AND — verified
    # 2026-07-26: `portable m3 command` matches docs containing all three in
    # ANY order, and does NOT match a doc with only one.
    #
    # This previously fell through to `" | ".join(toks)` — the SAME OR as fts5
    # mode — so `search_mode` was accepted and SILENTLY IGNORED on PostgreSQL
    # for unquoted multi-token queries, while SQLite honoured it.
    #
    # `&` and NOT `<->` (FOLLOWED BY): <-> is adjacency, which is STRICTER than
    # FTS5's implicit AND and would reject the scrambled-order document SQLite
    # accepts — trading one parity break for its mirror image. Adjacency is
    # reserved for the EXPLICITLY QUOTED branch above, which is where FTS5 puts
    # it too.
    return " & ".join(toks), True


# ──────────────────────────────────────────────────────────────────────────────
# Title overlap (ranker helper)
# ──────────────────────────────────────────────────────────────────────────────
