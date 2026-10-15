"""Resident vector index for SQLite semantic candidates (Rust core ``VectorIndex``).

Semantic search on SQLite without sqlite-vec read every in-scope embedding blob
on every query and scored them all: about half of a ~200 ms search on a 21k
store, and the cost that grows with the store (0.45 s at 50k memories, 1.55 s
at 150k; measured 2026-10-15). This keeps the vectors in memory instead, int8
quantised at a quarter of the float32 size, and asks only for the k nearest.
The caller then reads just those rows and scores them with exact cosine, so the
index decides which rows are considered, never their scores.

Scope stays in SQL: each search runs the caller's own predicates as an id-only
query (rowid and content_hash, no blobs) and restricts the index to those rows.
That same query keeps the index fresh: a row it has not seen, or whose
content_hash changed (pg_sync's pull rewrites embeddings IN PLACE under the
same rowid), is loaded before the search. A deleted row is simply never in the
allowed set again.

Without a core that has ``VectorIndex`` (or with ``M3_CORE_RS_DISABLE``),
``nearest`` returns None and the caller keeps the full scan.
"""

from __future__ import annotations

import json
import logging
import threading

from m3_core.errors import log_swallowed

from . import config

logger = logging.getLogger("memory.vector_index")

_LOCK = threading.Lock()
# (db_path, dim) -> (index, {rowid: content_hash it was loaded with})
_INDEXES: dict[tuple[str, int], tuple[object, dict[int, str | None]]] = {}
_LOAD_CHUNK = 500
_UNSEEN = object()


def available() -> bool:
    """True when the loaded Rust core has ``VectorIndex``."""
    rs = config.m3_core_rs
    return rs is not None and hasattr(rs, "VectorIndex")


def _index_for(db_path: str, dim: int):
    rs = config.m3_core_rs
    if rs is None or not hasattr(rs, "VectorIndex"):
        return None
    with _LOCK:
        entry = _INDEXES.get((db_path, dim))
        if entry is None:
            entry = (rs.VectorIndex(dim), {})
            _INDEXES[(db_path, dim)] = entry
        return entry


def nearest(db, *, dim: int, where_sql: str, params, q_vec, k: int) -> list[int] | None:
    """Rowids of the ``k`` nearest in-scope embedding rows, nearest first, or
    None when no index is available (the caller then scans in full).

    The index is keyed by the database file ``db`` has open.
    ``where_sql``/``params`` are the caller's scope predicates over
    ``memory_items mi`` joined to ``memory_embeddings me``.
    """
    if not available():
        return None
    try:
        # Keyed by the file this connection has open, never by a default path:
        # rowids of two stores would collide in one index.
        db_path = db.execute("PRAGMA database_list").fetchone()[2] or ""
        entry = _index_for(db_path, dim)
        if entry is None:
            return None
        return _nearest(entry, db, dim=dim, where_sql=where_sql, params=params, q_vec=q_vec, k=k)
    except Exception as e:  # noqa: BLE001 — the full scan still answers, and the cause is logged
        log_swallowed(logger, e, "vector index unavailable for this search (%s); scanning in full", e)
        return None


def _nearest(entry, db, *, dim: int, where_sql: str, params, q_vec, k: int) -> list[int]:
    index, seen = entry
    scope = db.execute(
        "SELECT me.rowid, me.content_hash FROM memory_items mi "
        f"JOIN memory_embeddings me ON mi.id = me.memory_id WHERE {where_sql}",
        params,
    ).fetchall()
    with _LOCK:
        stale = [rid for rid, h in scope if seen.get(rid, _UNSEEN) != h]
    for i in range(0, len(stale), _LOAD_CHUNK):
        chunk = stale[i : i + _LOAD_CHUNK]
        rows = db.execute(
            "SELECT rowid, embedding, content_hash FROM memory_embeddings "
            "WHERE rowid IN (SELECT value FROM json_each(?))",
            (json.dumps(chunk),),
        ).fetchall()
        good = [(rid, blob, h) for rid, blob, h in rows if blob is not None and len(blob) == dim * 4]
        if good:
            index.upsert_packed([r[0] for r in good], b"".join(r[1] for r in good))
        with _LOCK:
            for rid, _blob, h in rows:
                seen[rid] = h  # a wrong-length blob is recorded too, so it is not re-read every search
    if stale:
        logger.debug("vector index: loaded %d row(s); %d resident", len(stale), len(index))
    hits = index.search(list(q_vec), k, [rid for rid, _h in scope])
    return [rid for rid, _score in hits]


def reset() -> None:
    """Drop every resident index (tests; a store replaced on disk)."""
    with _LOCK:
        _INDEXES.clear()
