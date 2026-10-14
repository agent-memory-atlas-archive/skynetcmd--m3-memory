"""In-database vector copy probe: can vector search score inside the database?

On PostgreSQL with pgvector, each embedding also has a ``vector`` copy (pg_066),
and search can score in the database instead of shipping every vector to m3.
It may do that only for a table where EVERY row has its copy; one row without
it would be missed. This probe says which state the store is in and what moves
it forward.

Report-only (always returns 0): the baseline path is supported and correct, so a
server without pgvector is information, not a fault. With ``fix=True``
(``m3 doctor --fix``) it re-applies pg_066 when that has something to do (for
example after a DBA ran ``CREATE EXTENSION vector`` on a store whose migration
was already recorded) and fills every missing copy, one committed batch at a
time so no long lock is held.

A backend that keeps no copy (SQLite scores the blob directly) prints nothing in
brief mode.
"""

from __future__ import annotations

import logging

from m3_core.errors import log_swallowed

logger = logging.getLogger("memory.doctor.vector_copy_probe")

_FILL_BATCH = 1000


def _fill_all(backend, conn, tables) -> dict[str, int]:
    filled: dict[str, int] = {}
    for table in tables:
        total = 0
        while n := backend.fill_vector_copy(conn, table=table, limit=_FILL_BATCH):
            conn.commit()
            total += n
        filled[table] = total
    return filled


def run(brief: bool = False, fix: bool = False) -> int:
    """Report (and with ``fix``, repair) the vector copy. Always 0."""
    try:
        from memory.backends import active_backend

        backend = active_backend()
        with backend.connection() as conn:
            status = backend.vector_copy_status(conn)
            if status is None:
                if not brief:
                    print()
                    print("=== vector copy ===")
                    print("  status   : none on this backend (stored vectors are scored directly)")
                return 0
            filled: dict[str, int] = {}
            if fix:
                backend.ensure_vector_copy(conn)
                conn.commit()
                # A backend that kept a copy a moment ago still does; `or status`
                # only narrows the type.
                status = backend.vector_copy_status(conn) or status
                filled = _fill_all(backend, conn, list(status.missing))
                status = backend.vector_copy_status(conn) or status
    except Exception as e:  # noqa: BLE001 — a probe must never crash the doctor
        log_swallowed(logger, e, f"vector copy probe failed: {e}")
        print(f"⚠️  pgvector: could not read its state ({type(e).__name__}: {e}) — inspect: `m3 doctor --verbose`")
        return 0

    lacking = sum(status.missing.values())
    if not status.available:
        line = "ℹ️  pgvector: not offered by this PostgreSQL server (optional; search scores vectors in m3)"
    elif not status.installed:
        line = (
            "ℹ️  pgvector: offered but not created in this database (optional) — to enable: "
            "as a superuser, CREATE EXTENSION vector; then `m3 doctor --fix`"
        )
    elif "memory_embeddings" not in status.missing:
        line = "⚠️  pgvector: created, but the vector column is missing — fix: `m3 doctor --fix`"
    elif lacking:
        line = (
            f"⚠️  pgvector: {lacking} embedding(s) lack their vector copy "
            "— fix: `m3 doctor --fix` (the cognitive loop also fills them)"
        )
    else:
        line = "✅ pgvector: every embedding has its vector copy"
    print(line)
    if not brief:
        for table, n in sorted(status.missing.items()):
            print(f"  {table:<20}: {n} row(s) without a vector copy")
        for table, n in sorted(filled.items()):
            print(f"  filled   : {n} row(s) in {table}")
    return 0
