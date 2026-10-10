#!/usr/bin/env python3
"""Backfill `observation_queue` from existing chatlog rows.

Scans `memory_items WHERE type='chat_log'` in the chatlog DB, groups by
conversation_id, and INSERTs one row per conversation into `observation_queue`
of the main DB. Order is reverse-chronological (newest conversations first),
so when the periodic drain runs it processes the most recent material first.

Idempotent: the dialect's insert-or-ignore, so re-running won't duplicate
enqueued work for conversations still in the queue. Conversations already drained out
of the queue WILL be re-enqueued — use `--skip-already-enriched` to also
exclude conversations that already produced observations under the configured
target variant.

Every backend: on SQLite the chatlog is its own file (--chatlog-db) and the
queue is in the main file (--main-db); on PostgreSQL both are tables of the one
configured database and the path flags are refused.
"""

from __future__ import annotations

import argparse
import sys

from m3_core.context import pointed_store_connection
from m3_core.paths import resolve_cli_store, seam_backend, seam_dialect

# Date bounds go through the shared seam normalizer: a bare YYYY-MM-DD compared
# against an ISO-TEXT timestamp column is lexicographically wrong (an `until`
# excluded the entire requested day). See memory/backends/dialect.py.
try:  # pragma: no cover - import shim for standalone execution
    from memory.backends.dialect import Dialect as _Dialect

    _normalize_date_bound = _Dialect.normalize_date_bound
    _date_bound_op = _Dialect.date_bound_op
except ImportError:  # pragma: no cover - bootstrap: no memory package
    import re as _re_db
    from datetime import date as _d_db
    from datetime import timedelta as _td_db

    _BARE = _re_db.compile(r"^\d{4}-\d{2}-\d{2}$")

    def _normalize_date_bound(value: str, side: str) -> str:
        v = (value or "").strip()
        if not _BARE.match(v):
            return v
        if side == "since":
            return v + "T00:00:00Z"
        y, m, d = (int(x) for x in v.split("-"))
        return (_d_db(y, m, d) + _td_db(days=1)).strftime("%Y-%m-%dT00:00:00Z")

    def _date_bound_op(side: str) -> str:
        return ">=" if side == "since" else "<"


def _ensure_observation_queue(con) -> None:
    """Verify the queue table exists; we don't create it (migrations own that)."""
    sql, params = seam_dialect().table_exists("observation_queue")
    if con.execute(sql, params).fetchone() is None:
        raise SystemExit(
            "observed: the main store has no observation_queue table. "
            "inspect: run the migrations (m3 setup, or bin/migrate_memory.py / bin/migrate_pg.py)"
        )


def _conversations_reverse_chrono(
    chatlog_db: str | None,
    since: str | None,
) -> list[tuple[str, str, str]]:
    """Return (conversation_id, user_id, last_seen) tuples, newest first.

    Reads `memory_items` rows where `type='chat_log'`, grouped by
    conversation_id, ordered by most-recent turn timestamp descending.
    """
    from memory.backends.dialect import chatlog_table

    items = chatlog_table("items")
    with seam_backend().open_readonly(str(chatlog_db or "")) as con:
        params: list[object] = []
        where = "WHERE type='chat_log' AND conversation_id IS NOT NULL AND conversation_id != ''"
        if since:
            where += f" AND created_at {_date_bound_op('since')} {seam_dialect().param()}"
            params.append(_normalize_date_bound(since, "since"))
        cur = con.execute(
            f"""
            SELECT conversation_id,
                   COALESCE(MAX(user_id), '') AS user_id,
                   MAX(created_at) AS last_seen
            FROM {items}
            {where}
            GROUP BY conversation_id
            ORDER BY last_seen DESC
            """,
            params,
        )
        return [(r[0], r[1] or "", r[2]) for r in cur.fetchall()]


def _already_enriched(
    main_db: str | None,
    target_variant: str,
) -> set[str]:
    """Return conversation_ids that already have observations under variant."""
    with seam_backend().open_readonly(str(main_db or "")) as con:
        cur = con.execute(
            "SELECT DISTINCT conversation_id FROM memory_items "
            f"WHERE variant={seam_dialect().param()} AND conversation_id IS NOT NULL",
            (target_variant,),
        )
        return {r[0] for r in cur.fetchall() if r[0]}


def _chatlog_default() -> str | None:
    """The configured chatlog store's path on SQLite (its own resolver)."""
    try:
        from chatlog_config import chatlog_db_path
    except ImportError:  # bootstrap: no chatlog layer; fall back to the main store
        return None
    return chatlog_db_path()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enqueue chatlog conversations into observation_queue, newest first.",
    )
    parser.add_argument(
        "--main-db", default=None, help="Main SQLite DB (default: the active store). Not used on PostgreSQL."
    )
    parser.add_argument(
        "--chatlog-db",
        default=None,
        help="Chatlog SQLite DB (default: the configured chatlog store). Not used on PostgreSQL.",
    )
    parser.add_argument(
        "--since", default=None, help="Only enqueue conversations with rows since this ISO timestamp (e.g. 2026-04-01)."
    )
    parser.add_argument(
        "--skip-already-enriched",
        default=None,
        metavar="VARIANT",
        help="Skip conversation_ids already present under this variant in memory_items.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap how many conversations to enqueue.")
    parser.add_argument("--dry-run", action="store_true", help="Count what would be enqueued, write nothing.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    try:
        main_db = resolve_cli_store(args.main_db, "--main-db")
        chatlog_db = resolve_cli_store(args.chatlog_db, "--chatlog-db", default=_chatlog_default())
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    convos = _conversations_reverse_chrono(chatlog_db, args.since)
    print(f"found {len(convos)} conversations in chatlog (since={args.since or 'beginning'})")

    skip = set()
    if args.skip_already_enriched:
        skip = _already_enriched(main_db, args.skip_already_enriched)
        print(f"  excluding {len(skip)} already enriched under variant={args.skip_already_enriched!r}")

    candidates = [(cid, uid, ts) for cid, uid, ts in convos if cid not in skip]
    if args.limit:
        candidates = candidates[: args.limit]

    print(f"  -> {len(candidates)} candidates to enqueue (reverse chronological)")
    if not candidates:
        return 0
    print(f"     newest: {candidates[0][2]}  ({candidates[0][0][:12]}...)")
    print(f"     oldest: {candidates[-1][2]}  ({candidates[-1][0][:12]}...)")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    if not args.yes:
        try:
            resp = (
                input(
                    f"\nenqueue {len(candidates)} rows into "
                    f"{main_db or seam_backend().name}::observation_queue ? [y/N]: "
                )
                .strip()
                .lower()
            )
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("aborted.")
            return 0

    d = seam_dialect()
    with pointed_store_connection(main_db) as con:
        _ensure_observation_queue(con)
        cur = con.cursor()
        # Lock now, not at the first write: the SELECT above chose these
        # candidates and another pass must not enqueue them in between.
        d.begin_immediate(cur)
        rowcount = 0
        # "INSERT OR IGNORE" is a SQLite verb form; PostgreSQL puts the arbiter
        # in a trailing ON CONFLICT clause. The dialect pair renders both --
        # verified against a live PG 16.14: the generated
        # "INSERT INTO ... VALUES (%s, %s) ON CONFLICT DO NOTHING" left exactly
        # one row after a duplicate insert.
        _p = d.param()
        sql = (
            f"{d.insert_or_ignore()} observation_queue (conversation_id, user_id) "
            f"VALUES ({_p}, {_p}) {d.on_conflict_ignore()}"
        )
        for cid, uid, _ts in candidates:
            cur.execute(sql, (cid, uid))
            rowcount += cur.rowcount
        con.commit()
        print(f"\nenqueued {rowcount} new rows ({len(candidates) - rowcount} were already in the queue).")

    print("\nnext step: the AgentOS_ObservationDrain scheduled task will process them on its next fire (every 15 min).")
    print("or run manually:")
    print("  python bin/m3_enrich.py --drain-queue --drain-batch 200 --profile enrich_local_qwen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
