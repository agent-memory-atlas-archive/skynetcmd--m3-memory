#!/usr/bin/env python3
"""
m3_enrich_assign.py — assign enrichment_groups.send_to for routed runs.

Updates the `send_to` column on rows matching a predicate, so that a
later `m3_enrich.py --send-to <name>` run claims only its assigned
rows. Use when running multiple providers (e.g. Grok + Gemini) against
the same source variant in parallel and you want disjoint pools by
explicit assignment rather than by accidental bucket isolation.

Common patterns:

    # Route all groups <= 7 KB to "grok" (--db only on SQLite; PostgreSQL
    # uses the configured database)
    m3_enrich_assign --db memory/other.db \\
                     --source-variant my-source-variant \\
                     --target-variant my-target-variant \\
                     --max-size-k 7 --send-to grok

    # Route the rest (>= 16 KB) to "gemini", leaving the 8-15 KB middle
    # band unassigned (NULL) for later
    m3_enrich_assign --db memory/other.db \\
                     --source-variant my-source-variant \\
                     --target-variant my-target-variant \\
                     --min-size-k 16 --send-to gemini

    # Dry-run first
    m3_enrich_assign ... --send-to grok --dry-run

The script only touches `pending` and `failed` rows by default — it
won't reassign rows that already succeeded. Use --include-completed to
override.
"""
from __future__ import annotations

import argparse
import sys

from m3_core.context import pointed_store_connection
from m3_core.paths import resolve_cli_store, seam_dialect


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None,
                    help="SQLite database with enrichment_groups (default: the "
                         "active store). Not used on PostgreSQL.")
    ap.add_argument("--source-variant", required=True,
                    help="Match enrichment_groups.source_variant.")
    ap.add_argument("--target-variant", required=True,
                    help="Match enrichment_groups.target_variant.")
    ap.add_argument("--send-to", required=True,
                    help="Provider name to assign (e.g. 'grok', 'gemini'). "
                         "Pass the literal string 'NULL' to clear assignments.")
    ap.add_argument("--min-size-k", type=int,
                    help="Only assign rows whose content_size_k >= N.")
    ap.add_argument("--max-size-k", type=int,
                    help="Only assign rows whose content_size_k <= N.")
    ap.add_argument("--only-unassigned", action="store_true",
                    help="Only assign rows where send_to IS NULL. Use to "
                         "avoid clobbering an existing routing scheme.")
    ap.add_argument("--include-completed", action="store_true",
                    help="Also reassign rows in success/empty/dead_letter "
                         "status. Default: only pending and failed.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be updated; don't write.")
    args = ap.parse_args()

    try:
        db_path = resolve_cli_store(args.db)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    with pointed_store_connection(db_path) as conn:
        return _assign(conn, args, db_path)


def _assign(conn, args, db_path: str | None) -> int:
    d = seam_dialect()
    p = d.param()
    # Verify the column exists (migration 031; PostgreSQL pg_060).
    col_sql, col_params = d.columns_of("enrichment_groups")
    if "send_to" not in {r[0] for r in conn.execute(col_sql, col_params).fetchall()}:
        print("ERROR: observed: enrichment_groups.send_to does not exist on this store. "
              "inspect: apply the migrations (SQLite 031: "
              f"python bin/migrate_memory.py --db {db_path or '<db>'} up; PostgreSQL pg_060: "
              "m3 setup or bin/migrate_pg.py)", file=sys.stderr)
        return 2

    # Build the WHERE clause.
    where = [f"source_variant = {p}", f"target_variant = {p}"]
    params: list = [args.source_variant, args.target_variant]
    if args.min_size_k is not None:
        where.append(f"content_size_k >= {p}")
        params.append(args.min_size_k)
    if args.max_size_k is not None:
        where.append(f"content_size_k <= {p}")
        params.append(args.max_size_k)
    if args.only_unassigned:
        where.append("send_to IS NULL")
    if not args.include_completed:
        where.append("status IN ('pending', 'failed')")
    where_sql = " AND ".join(where)

    # Preview: count + size-band breakdown.
    n_match = conn.execute(
        f"SELECT COUNT(*) FROM enrichment_groups WHERE {where_sql}", params,
    ).fetchone()[0]

    if n_match == 0:
        print("0 rows match. Nothing to assign.")
        return 0

    print(f"Matched: {n_match} rows")
    print()
    print("Status breakdown:")
    for r in conn.execute(
        f"SELECT status, COUNT(*) FROM enrichment_groups WHERE {where_sql} "
        f"GROUP BY status ORDER BY 2 DESC", params,
    ):
        print(f"  {r[0]:<15} {r[1]}")
    print()
    print("Current send_to breakdown (pre-update):")
    for r in conn.execute(
        f"SELECT send_to, COUNT(*) FROM enrichment_groups WHERE {where_sql} "
        f"GROUP BY send_to ORDER BY 2 DESC", params,
    ):
        label = r[0] if r[0] is not None else "(NULL)"
        print(f"  {label:<15} {r[1]}")
    print()

    new_value: str | None = None if args.send_to.upper() == "NULL" else args.send_to
    print(f"New send_to value: {new_value!r}")

    if args.dry_run:
        print("(dry-run: no changes written)")
        return 0

    # Apply.
    n_updated = conn.execute(
        f"UPDATE enrichment_groups SET send_to = {p} WHERE {where_sql}",
        [new_value, *params],
    ).rowcount
    conn.commit()
    print(f"Updated {n_updated} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
