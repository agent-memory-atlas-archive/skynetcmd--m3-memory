"""Where a search spends its time: the retrieval stage map for m3_core.monitoring.

``search_profile_impl`` runs one real ``memory_search_scored_impl`` under
``stage_timing`` and reports per stage: embed (the query vector), hybrid_score,
rank (recency / temporal / elbow), mmr (the Rust kernel), rerank, expand,
fts_fallback, db (driver execute/fetch calls and the PG compat wrappers), and
total. ``unattributed_ms`` is total's SELF time: work done inline in the search
function, outside every named stage. Instrumentation is local to these
functions and exists only for the duration of the call (sys.monitoring, PEP 669).
"""

from __future__ import annotations

import time
from typing import Any

from m3_core.monitoring import StageMap, c_target, stage_timing

# A driver call is DB time when its owner is one of these modules.
_DB_MODULES = ("sqlite3", "_sqlite3", "psycopg2", "psycopg")
_DB_CALLS = frozenset({"execute", "executemany", "executescript", "fetchone", "fetchall", "fetchmany"})


def _classify_c(callable_: object, arg0: object) -> str | None:
    module, name = c_target(callable_, arg0)
    if name in _DB_CALLS and module.split(".")[0] in _DB_MODULES:
        return "db"
    if module.split(".")[0] == "m3_core_rs" and name.startswith("mmr"):
        return "mmr"
    return None


def retrieval_stage_map() -> StageMap:
    """The stages of memory_search_scored_impl. Imported on call, so loading this
    module costs nothing until a profile is requested."""
    from memory import db, embed, fts, search, search_ranking, search_routing
    from memory.backends import postgres_backend, sqlite_backend

    functions: dict[str, list[Any]] = {
        "total": [search.memory_search_scored_impl],
        "embed": [embed.embed_for_search],  # the search path's one query-vector entry
        "hybrid_score": [search_ranking._hybrid_score_batch],
        "rank": [
            search_ranking._apply_recency_bonus,
            search_ranking._apply_temporal_boost,
            search_ranking._trim_by_elbow,
        ],
        "rerank": [search._apply_rerank],
        "expand": [
            search._apply_two_stage_expansion,
            search._apply_observation_preference,
            search_routing._pull_predecessor_turns,
            search_routing._enforce_expansion_displacement_guard,
        ],
        "fts_fallback": [search._fts_only_results],
        "db": [postgres_backend._SqliteCompatConnection.execute, postgres_backend._SqliteCompatConnection.executemany],
    }
    # Driver C calls are counted wherever the search path makes them: the search
    # modules' helpers and the backends' own query methods (keyword search runs
    # its SQL there). A driver call inside the PG compat execute (already the db
    # stage) is not counted again (m3_core.monitoring: no C stage nests in itself).
    # The LIVE backend's class too: the selector caches an instance whose class
    # may come from an earlier load of its module, so the module object by name
    # is not always the code that runs (seen as an order-dependent miss).
    from memory.backends import active_backend

    return StageMap(
        functions=functions,
        c_call_modules=[
            search,
            search_ranking,
            search_routing,
            fts,
            db,
            sqlite_backend,
            postgres_backend,
            type(active_backend()),
        ],
        classify_c=_classify_c,
    )


async def search_profile_impl(
    query: str, k: int = 8, search_mode: str = "hybrid", user_id: str = "", scope: str = ""
) -> dict[str, Any]:
    """Run one search with per-stage timing; return the hit count and the timings."""
    from memory.search import memory_search_scored_impl

    started = time.perf_counter()
    with stage_timing(retrieval_stage_map()) as report:
        hits = await memory_search_scored_impl(query, k=k, search_mode=search_mode, user_id=user_id, scope=scope)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    stages = report.as_dict()
    total = stages.get("total", {})
    return {
        "query": query,
        "hits": len(hits) if isinstance(hits, list) else None,
        "elapsed_ms": elapsed_ms,
        "unattributed_ms": total.get("self_ms"),
        "stages": stages,
        "note": (
            "wall-clock per stage; self_ms excludes nested stages in the same task. "
            "unattributed_ms is inline work in the search function outside every stage."
        ),
    }
