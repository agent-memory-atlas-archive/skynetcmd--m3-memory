"""Pure routing helpers extracted from `memory.search` (Phase 4.B follow-up).

Holds the query-routing / result-shaping helpers that do not touch any
memory_core callback, the reranker singleton, or a `_resolve_*` shim:
temporal query classification, AUTO-branch param resolution, sharp-trim,
the expansion-displacement guard, sqlite-vec feature detection, and the
predecessor-turn pull (which talks to the DB directly via `.db._db`, not
through the floor-bound/test-shim pattern the big impls use).

CONTRACT: this module must NOT import `memory.search` (that would create a
cycle, since search.py re-imports these names at its top to keep the
memory_core lazy-registry shim resolving to the same function objects).
"""

from __future__ import annotations

import json
import logging

from m3_core.errors import log_swallowed

from . import config

# The sqlite-vec capability probe is the SQLite backend's (the database seam);
# re-exported for the search path. Explicit `as` form: a ruff F401 fix must
# not strip it.
from .backends.sqlite_backend import _detect_sqlite_vec as _detect_sqlite_vec
from .db import _db
from .fts import _EVENT_PROPER_NOUN, _TEMPORAL_QUERY_RE, _TEMPORAL_ROUTER_RE

logger = logging.getLogger("memory.search_routing")


def _pull_predecessor_turns(
    scored: list, user_id: str = "", scope: str = "", requesting_agent: str = "", as_of: str = ""
) -> None:
    """Append turn N-1 to ``scored`` when turn N is already present.

    Used under M3_INTENT_ROUTING with intent_hint="user-fact" — bridges
    the gap where the assistant echo is the best FTS match but the
    user's original statement (one turn earlier) carries the actual
    fact. Mutates the list in-place with the predecessor scored at
    ~85% of the original turn's score so it competes but doesn't
    automatically displace.

    Caps at the top 10 current hits to bound extra DB work; most
    user-fact queries only need a few predecessors, not a bulk pull.
    Items without ``conversation_id`` or ``metadata_json.turn_index``
    are skipped.
    """
    candidates: list[tuple[str, int, float]] = []  # (cid, target_idx, parent_score)
    seen_ids = {item.get("id") for _, item in scored if item.get("id")}
    for score, item in scored[:10]:
        cid = item.get("conversation_id")
        meta_raw = item.get("metadata_json")
        if not cid or not meta_raw:
            continue
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            t_idx = meta.get("turn_index")
            if isinstance(t_idx, int) and t_idx > 0:
                candidates.append((cid, t_idx - 1, score))
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    if not candidates:
        return
    # Single batched query: pull all turns from the affected conversations in
    # one round-trip, then filter to the exact (cid, turn_index) pairs and the
    # per-candidate parent_score in Python.
    cids: set[str] = {c for c, _, _ in candidates}
    wanted: dict[tuple[str, int], float] = {}
    for cid, t_idx, p_score in candidates:
        key = (cid, t_idx)
        # Multiple top-10 hits in the same conv may share a target_idx; keep
        # the parent_score of the higher-ranked hit (first occurrence wins).
        wanted.setdefault(key, p_score)
    try:
        # Seam primitives: literal '?' / json_extract() are SQLite-only and raise
        # on PostgreSQL. Route binds through param()/placeholder() and the JSON
        # cast through json_extract_int() so this executes on both backends.
        from .backends import dialect as _dialect

        _d = _dialect()
        _p = _d.param()
        with _db() as db:
            placeholders = _d.placeholder(len(cids))
            # TENANCY (§7): conversation_id is per-conversation isolation, NOT
            # per-tenant or per-agent. Re-apply the caller's visibility and
            # isolation (Dialect.row_visibility) so a shared/forged conversation_id
            # can't hydrate another tenant's or agent's private turn N-1. Empty
            # values pass through unchanged (legacy shared-agent path).
            _tenancy_sql, _tenancy_params = _d.row_visibility(
                "mi", user_id=user_id, scope=scope, requesting_agent=requesting_agent, as_of=as_of
            )
            rows = db.execute(
                f"SELECT id, content, title, type, importance, metadata_json, "
                f"  conversation_id, "
                f"  {_d.json_extract_int('metadata_json', 'turn_index')} AS turn_index "
                f"FROM memory_items mi "
                f"WHERE conversation_id IN ({placeholders}){_tenancy_sql}",
                tuple(cids) + tuple(_tenancy_params),
            ).fetchall()
        for row in rows:
            tkey = (row["conversation_id"], row["turn_index"])
            if tkey not in wanted:
                continue
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            pre_item = {
                "id": row["id"],
                "content": row["content"],
                "title": row["title"],
                "type": row["type"],
                "importance": row["importance"],
                "metadata_json": row["metadata_json"],
                "conversation_id": row["conversation_id"],
            }
            scored.append((wanted[tkey] * 0.85, pre_item))
    except Exception as e:  # defensive — predecessor pull is best-effort
        log_swallowed(logger, e, f"predecessor pull skipped: {type(e).__name__}: {e}")


def _maybe_route_query(query: str, vector_weight: float, intent_hint: str = "") -> float:
    """Decide whether to shift vector_weight toward BM25 based on query shape.

    Two triggers — an SLM-supplied intent hint takes precedence, then the
    heuristic fires as a fallback:
      - intent_hint in {"temporal-reasoning", "multi-session"} -> 0.3
      - QUERY_TYPE_ROUTING on AND query starts with "when/what date/..."
        AND contains a proper noun -> 0.3
    Both require the M3_QUERY_TYPE_ROUTING env gate. intent-hint path
    ALSO works standalone when M3_INTENT_ROUTING is on (so bench callers
    can opt in without touching both knobs).
    """
    # Intent-hint path: trusted signal from an upstream classifier.
    if intent_hint and (config.QUERY_TYPE_ROUTING or config.INTENT_ROUTING):
        if intent_hint in ("temporal-reasoning", "multi-session"):
            return 0.3
    # Heuristic path: unchanged from before.
    if not config.QUERY_TYPE_ROUTING:
        return vector_weight
    if not query:
        return vector_weight
    if not _TEMPORAL_QUERY_RE.search(query):
        return vector_weight
    if not _EVENT_PROPER_NOUN.search(query):
        return vector_weight
    return 0.3


def _enforce_expansion_displacement_guard(
    hits: list,
    *,
    protected_ranks: int | None = None,
    margin: float | None = None,
) -> list:
    """Enforce: at ranks 1..protected_ranks, expansion rows may only outrank a
    primary row if expansion_score >= margin * primary_score.

    Operates on a list[tuple[score, dict]] in current ranked order. Items are
    classified as "expansion" if dict["_expanded_via"] is set and != "primary";
    everything else (including missing tag, "primary") is treated as primary.

    The pass walks rank 1..protected_ranks. At each protected rank, if the row
    is an expansion that fails the margin test against the next primary row in
    the list, swap them. The same primary is then locked at that rank; we move
    on to the next protected rank. Beyond protected_ranks, the original order
    is preserved.

    Idempotent on already-conforming lists. No-op if protected_ranks <= 0 or
    margin <= 1.0 (treating margin <= 1.0 as "no displacement allowed at all"
    would be too strict; instead we treat it as "feature disabled, score-only").

    Unset arguments read search.expansion_protected_ranks /
    search.expansion_displacement_margin at CALL time (a default argument would
    bind the value at import and miss the config file and the test fixture).
    """
    if protected_ranks is None:
        protected_ranks = config.EXPANSION_PROTECTED_RANKS
    if margin is None:
        margin = config.EXPANSION_DISPLACEMENT_MARGIN
    if not hits or protected_ranks <= 0 or margin <= 1.0:
        return hits

    def _is_expansion(item) -> bool:
        if not isinstance(item, dict):
            return False
        tag = item.get("_expanded_via")
        return bool(tag) and tag != "primary"

    # Rust path: classification stays here (it knows _expanded_via); the Rust
    # core computes the reordering permutation, which we apply to the original
    # (score, item) rows.
    if config.m3_core_rs is not None:
        typed = [(float(s), _is_expansion(it)) for s, it in hits]
        perm = config.m3_core_rs.enforce_displacement_guard(typed, protected_ranks, margin)
        return [hits[i] for i in perm]

    work = list(hits)
    n = len(work)
    limit = min(protected_ranks, n)
    for rank in range(limit):
        score, item = work[rank]
        if not _is_expansion(item):
            continue
        # Find the next primary candidate at rank+1..end
        next_primary_idx = None
        for j in range(rank + 1, n):
            if not _is_expansion(work[j][1]):
                next_primary_idx = j
                break
        if next_primary_idx is None:
            continue
        primary_score, _ = work[next_primary_idx]
        if score > 0 and primary_score > 0 and score >= margin * primary_score:
            continue  # expansion earned its rank
        work[rank], work[next_primary_idx] = work[next_primary_idx], work[rank]
    return work


def _extract_caller_overrides(local_args: dict, sig_defaults: dict) -> dict:
    """Return only params the caller actually changed from function-signature defaults.

    local_args: the dict of param names → values actually in use (e.g. a subset of locals())
    sig_defaults: dict of param_name -> default_value from the function's signature

    A value is considered an override when it differs from the signature default by
    identity or equality.  String/numeric/bool comparisons use ==; object sentinels
    use `is not`.
    """
    overrides = {}
    for k, v in local_args.items():
        if k not in sig_defaults:
            continue
        default = sig_defaults[k]
        # Use identity check first (catches sentinel objects), then equality.
        if v is not default and v != default:
            overrides[k] = v
    return overrides


def _apply_auto_layer(
    query: str,
    primary_candidates: list,
    current_params: dict,
    sig_defaults: dict,
) -> tuple:
    """Apply AUTO branch values to params. Caller overrides are always preserved.

    current_params: the kwargs dict reflecting what the caller actually passed
    sig_defaults: function-signature defaults (for override detection)

    Resolution order (lowest -> highest priority):
      1. sig_defaults         — function-signature concrete defaults
      2. branch_vals          — AUTO branch values for the chosen branch
      3. caller_overrides     — what the caller explicitly changed from defaults

    Returns:
        (resolved_params: dict, auto_metadata: dict)

    auto_metadata contains: auto_branch, auto_branch_values, caller_overrides, auto_signals
    """
    import auto_route  # local import avoids circular; auto_route has no memory_core deps

    branch = auto_route.decide_branch(query, primary_candidates, current_params)
    branch_vals = auto_route.branch_values(branch, current_params)
    caller_overrides = _extract_caller_overrides(current_params, sig_defaults)

    # Merge layers: defaults -> AUTO branch values -> caller overrides
    resolved = {**sig_defaults, **branch_vals, **caller_overrides}

    return resolved, {
        "auto_branch": branch,
        "auto_branch_values": branch_vals,
        "caller_overrides": caller_overrides,
        "auto_signals": auto_route.signals_summary(query, primary_candidates),
    }


def _apply_sharp_trim(hits, *, threshold_ratio, k_min, k_max):
    """Sharp-branch post-process: keep hits within threshold_ratio of top score, bounded [k_min, k_max].

    hits: list of (score, item_dict) tuples (the canonical routed_impl output shape)
    """
    if not hits:
        return hits
    if k_max and len(hits) > k_max:
        hits = hits[:k_max]
    top_score = hits[0][0] if hits else 0.0
    if top_score <= 0:
        return hits[: max(k_min, 1)]
    threshold = top_score * threshold_ratio
    kept = [h for h in hits if h[0] >= threshold]
    if k_min and len(kept) < k_min:
        kept = hits[:k_min]
    return kept


def is_temporal_query(query: str) -> bool:
    """Returns True if the query uses temporal vocabulary (regex-based, no LLM)."""
    if not query:
        return False
    return bool(_TEMPORAL_ROUTER_RE.search(query))


# ──────────────────────────────────────────────────────────────────────────────
# SQLite fused candidate-fetch SQL — ONE builder, four variants.
#
# `memory/search.py` assembled this SQL inline in FOUR places: semantic and
# hybrid, each × sqlite-vec present/absent. That duplication is the same shape
# that produced the 2026-07-26 filter bug — row-scoping predicates lived in four
# hand-maintained copies, a tenancy fix reached all four, type_filter/
# agent_filter reached one, and filtered searches silently returned unfiltered
# rows. Sharing a PREDICATE is not the same as having one code path.
#
# Kept a PURE function (str in, str out — no connection, no execution) so the
# extraction is provable: tests assert it reproduces the previous strings
# byte-for-byte. A builder that needs a live DB to test is a builder whose
# output nobody checks.
#
# SQLITE-ONLY BY CONSTRUCTION. FTS5 (`memory_items_fts`, `bm25()`) and
# sqlite-vec (`vec_distance_cosine`) are SQLite features; PostgreSQL reaches the
# same candidates through search_pg.fetch_candidates_pg (tsvector + pgvector).
# The literal `?` placeholders are therefore correct here rather than a seam
# violation — but `where_sql` still arrives from Dialect.scope_predicates so the
# predicates themselves stay singular. Do not "fix" the placeholders; fix the
# caller if this is ever reached off SQLite.
# ──────────────────────────────────────────────────────────────────────────────

_BASE_COLS = "mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding"


def build_candidate_sql(
    *,
    search_mode: str,
    has_vec: bool,
    where_sql: str,
    extra_sql: str = "",
    row_limit: int | None = None,
    key_filter: bool = False,
) -> str:
    """Build the SQLite fused candidate-fetch query. Pure; returns SQL text.

    ``search_mode`` "semantic" selects the vector-only shape (every in-scope
    embedding, no FTS; with sqlite-vec ranked and capped at ``row_limit`` in
    SQL, without it ranked by the caller); anything else selects the hybrid
    shape (FTS5 MATCH joined inline, bm25-ordered, capped at ``row_limit``).

    ``has_vec`` selects whether the cosine distance is computed IN SQL by
    sqlite-vec. It is an ACCELERATOR, not the vector engine: when False the same
    similarity is computed by the Rust core (`cosine_batch_packed`) from the raw
    blobs this query returns either way — verified equal to a reference cosine
    to ~3e-09. So the two variants differ in WHERE the arithmetic happens and
    how rows are ordered/limited in SQL, not in what similarity means.

    ``key_filter`` (semantic, no sqlite-vec): only the embedding rows whose
    rowids arrive as a JSON array -- the nearest ones from the resident index
    (memory.vector_index). The scope predicates still apply, so the index can
    narrow what a query sees but never widen it.

    Parameter ORDER is part of this contract, since callers bind positionally:
      semantic + has_vec : (q_blob, *where_params)
      semantic + keys    : (json_rowids, *where_params)
      semantic           : (*where_params,)
      hybrid   + has_vec : (fts_query, *where_params, q_blob, *where_params)
      hybrid             : (fts_query, *where_params, *where_params)
    (hybrid: the CTE consumes MATCH and the scope params, then the outer
    SELECT/WHERE consumes the vector blob and the scope params again).
    """
    if search_mode == "semantic":
        # Vector-only: every in-scope embedding is a candidate, ranked by
        # similarity. This used to take 50 items by the scoping predicates, in
        # no order, and rank only those: the same 50 rows for every query, so a
        # store of thousands answered from a fixed handful (measured 2026-10-15:
        # 0 of the 40 true nearest memories over 5 queries). The predicates also
        # name `me.*`, which inside that CTE bound to the OUTER join.
        if has_vec:
            return f"""
                            SELECT {_BASE_COLS}, 0.0 as bm25_score,
                                   (1.0 - vec_distance_cosine(me.embedding, ?)) as vec_score{extra_sql}
                            FROM memory_items mi
                            JOIN memory_embeddings me ON mi.id = me.memory_id
                            WHERE {where_sql}
                            ORDER BY vec_score DESC
                            LIMIT {row_limit}
                        """
        if key_filter:
            return f"""
                            SELECT {_BASE_COLS}, 0.0 as bm25_score{extra_sql}
                            FROM memory_items mi
                            JOIN memory_embeddings me ON mi.id = me.memory_id
                            WHERE me.rowid IN (SELECT value FROM json_each(?))
                              AND {where_sql}
                        """
        # No sqlite-vec: SQL cannot rank by similarity, so every in-scope row is
        # returned and the caller scores them all with the Rust cosine, then
        # keeps the best.
        return f"""
                            SELECT {_BASE_COLS}, 0.0 as bm25_score{extra_sql}
                            FROM memory_items mi
                            JOIN memory_embeddings me ON mi.id = me.memory_id
                            WHERE {where_sql}
                        """
    # Hybrid/fts5: rank on the FTS table ALONE, then join only the survivors.
    #
    # bm25() must score every FTS match before LIMIT applies, and ranking it
    # inside the joined query made SQLite carry the joins through that whole
    # match set. Measured on a 7,500-row store of real prose: a query for "the"
    # (5,266 matches) took 734.6 ms joined vs 29.2 ms pre-filtered -- 25x. The
    # gap widens as queries get MORE selective, because the CTE hands the joins
    # a tiny row set: "embedding" (115 matches) went 102.6 ms -> 0.7 ms (152x).
    #
    # Scope: the CTE applies `where_sql` (tenancy, live, embedder identity)
    # BEFORE the LIMIT, via EXISTS against the embeddings. Ranking the global
    # FTS match set first let other users' and deleted rows fill the cap: on a
    # 5-user store a 2,000-row cap kept 418 of 1,390 in-scope matches. The
    # outer WHERE repeats it to pick the matching embedding rows, so
    # `where_params` bind twice. Measured on 7,500 rows: no slower (3 ms vs 4).
    if has_vec:
        return f"""
                        WITH ranked AS (
                            SELECT memory_items_fts.rowid AS _rid, bm25(memory_items_fts) AS _b
                            FROM memory_items_fts
                            JOIN memory_items mi ON mi.rowid = memory_items_fts.rowid
                            WHERE memory_items_fts MATCH ?
                              AND EXISTS (
                                  SELECT 1 FROM memory_embeddings me
                                  WHERE me.memory_id = mi.id AND {where_sql}
                              )
                            ORDER BY _b ASC
                            LIMIT {row_limit}
                        )
                        SELECT {_BASE_COLS},
                               ranked._b as bm25_score,
                               (1.0 - vec_distance_cosine(me.embedding, ?)) as vec_score{extra_sql}
                        FROM ranked
                        JOIN memory_items mi ON mi.rowid = ranked._rid
                        JOIN memory_embeddings me ON mi.id = me.memory_id
                        WHERE {where_sql}
                        ORDER BY bm25_score ASC
                    """
    return f"""
                        WITH ranked AS (
                            SELECT memory_items_fts.rowid AS _rid, bm25(memory_items_fts) AS _b
                            FROM memory_items_fts
                            JOIN memory_items mi ON mi.rowid = memory_items_fts.rowid
                            WHERE memory_items_fts MATCH ?
                              AND EXISTS (
                                  SELECT 1 FROM memory_embeddings me
                                  WHERE me.memory_id = mi.id AND {where_sql}
                              )
                            ORDER BY _b ASC
                            LIMIT {row_limit}
                        )
                        SELECT {_BASE_COLS},
                               ranked._b as bm25_score{extra_sql}
                        FROM ranked
                        JOIN memory_items mi ON mi.rowid = ranked._rid
                        JOIN memory_embeddings me ON mi.id = me.memory_id
                        WHERE {where_sql}
                        ORDER BY bm25_score ASC
                    """
