#!/usr/bin/env python3
"""
backfill_content_hash.py — populate memory_embeddings.content_hash on legacy rows.

Why: the embed-cache lookup in memory_core._embed / _embed_many uses
`WHERE embed_model = ? AND content_hash IN (?, ...)`. Rows with NULL
content_hash are invisible to that cache — duplicate-text re-embeds
incur a fresh ~2080ms llama_encode roundtrip instead of a sub-ms hit.

Older write paths (and chatlog_embed_sweeper before commit d4b7b2c)
inserted memory_embeddings rows without populating content_hash. This
tool computes the hash from each row's source content (matching what
memory_core._content_hash would produce at write time) and UPDATEs the
embedding row in place.

Idempotent — re-running picks up only rows still NULL. Safe to run
alongside live writers (UPDATEs commit per batch; no schema change).

Every backend. On SQLite --db picks the file (the chatlog is its own file;
--target chatlog defaults to it). On PostgreSQL both stores are tables of the
one configured database: --target picks memory_* or chat_log_*, and --db is
refused. A PostgreSQL store migrated from an old SQLite one carries the NULL
hashes as copied, so the repair is needed there too.

Scope decision:
  - For chat_log / message rows, the embed text is the raw content
    (chatlog sweeper uses identity transform). Hash matches.
  - For other types, memory_write_impl applies _augment_embed_text_with_anchors
    to the content + metadata before hashing. To match exactly, we'd need
    to re-augment during backfill. We skip non-chat_log/message types by
    default (--types defaults to chat_log,message) so the backfilled
    hashes match what _embed_many computes for new embeds; pass --types
    explicitly with --augment-anchors to backfill other types with the
    augmentation transform applied.

Usage:

    # Default: only chat_log + message (raw-content hashes are safe)
    python bin/backfill_content_hash.py --db memory/agent_chatlog.db

    # Smoke test 100 rows
    python bin/backfill_content_hash.py --db memory/agent_chatlog.db --limit 100

    # Dry run — show counts, write nothing
    python bin/backfill_content_hash.py --db memory/agent_chatlog.db --dry-run

    # Custom type + augmented hashing (matches inline _embed behavior for non-chatlog)
    python bin/backfill_content_hash.py --db memory/agent_memory.db \\
        --type summary --type note --augment-anchors

    # Sharded across multiple invocations
    python bin/backfill_content_hash.py --db DB --id-prefix 0
    python bin/backfill_content_hash.py --db DB --id-prefix 1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import cast

from m3_core.context import pointed_store_connection
from m3_core.errors import is_defect
from m3_core.paths import pin_database, resolve_cli_store, seam_backend, seam_dialect  # noqa: E402
from m3_core.runtime import utc_iso

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


DEFAULT_TYPES = ("chat_log", "message")
DEFAULT_BATCH_SIZE = 1000


def _tables(args: argparse.Namespace) -> tuple[str, str]:
    """(items, embeddings) for --target, through the seam's chatlog naming."""
    if args.target == "chatlog":
        from memory.backends.dialect import chatlog_table

        return chatlog_table("items"), chatlog_table("embeddings")
    return "memory_items", "memory_embeddings"


def _verify_schema(conn, args: argparse.Namespace) -> None:
    """Confirm the items + embeddings tables exist with the required columns."""
    d = seam_dialect()
    items, embeddings = _tables(args)
    where = args.db or seam_backend().name
    for tbl, required in ((items, ("id", "content", "type")), (embeddings, ("content_hash",))):
        sql, params = d.table_exists(tbl)
        if conn.execute(sql, params).fetchone() is None:
            raise RuntimeError(
                f"observed: table {tbl!r} not found in {where}. "
                f"inspect: run the migrations (migrate_memory.py / migrate_pg.py)"
            )
        sql, params = d.columns_of(tbl)
        cols = {r[0] for r in conn.execute(sql, params).fetchall()}
        for col in required:
            if col not in cols:
                raise RuntimeError(
                    f"observed: {tbl}.{col} missing from {where}. inspect: the schema is too old; run the migrations"
                )


def _build_select(args: argparse.Namespace, after_id: str | None) -> tuple[str, list]:
    """Build the candidate-rows SELECT.

    Joins memory_embeddings to memory_items so we can filter on type /
    variant / user_id while reading the source content for hashing.
    Returns rows of (embedding_id, memory_id, content, metadata_json).
    """
    d = seam_dialect()
    p = d.param()
    items, embeddings = _tables(args)
    where = [
        "me.content_hash IS NULL",
        "COALESCE(mi.is_deleted, 0) = 0",
        d.has_content("mi.content"),
    ]
    params: list = []

    if after_id is not None:
        where.append(f"me.id > {p}")
        params.append(after_id)
    if args.type:
        where.append(f"mi.type IN ({d.placeholder(len(args.type))})")
        params.extend(args.type)
    if args.variant:
        where.append(f"mi.variant IN ({d.placeholder(len(args.variant))})")
        params.extend(args.variant)
    if args.user_id:
        where.append(f"COALESCE(mi.user_id, '') = {p}")
        params.append(args.user_id)
    if args.id_prefix:
        where.append(f"me.id LIKE {p}")
        params.append(f"{args.id_prefix.lower()}%")

    sql = f"""
        SELECT me.id, me.memory_id, mi.content, mi.metadata_json
        FROM {embeddings} me
        JOIN {items} mi ON mi.id = me.memory_id
        WHERE {" AND ".join(where)}
        ORDER BY me.id
        LIMIT {p}
    """
    return sql, params


def _count_pending(conn, args: argparse.Namespace) -> int:
    sql, params = _build_select(args, after_id=None)
    count_sql = sql.replace(
        "SELECT me.id, me.memory_id, mi.content, mi.metadata_json",
        "SELECT COUNT(*)",
    ).split("ORDER BY")[0]
    return cast("int", conn.execute(count_sql, params).fetchone()[0])


def _run_backfill(conn, args: argparse.Namespace) -> dict:
    """Run the backfill loop on ``conn``. Returns counters dict."""
    # Late import: memory_core's _content_hash + _augment_embed_text_with_anchors
    # need M3_DATABASE set BEFORE import (SQLite only: it names a file).
    if args.db:
        pin_database(str(args.db))
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    import memory_core as mc  # noqa: E402

    counters = {
        "scanned": 0,
        "updated": 0,
        "skipped_empty": 0,
        "errors": 0,
    }

    fetch_size = args.batch_size
    after_id: str | None = None
    started = time.monotonic()

    # One connection: each batch is fully fetched before it is written.
    _, embeddings = _tables(args)
    update_sql = f"UPDATE {embeddings} SET content_hash = {seam_dialect().param()} WHERE id = {seam_dialect().param()}"
    while True:
        if args.limit and counters["updated"] >= args.limit:
            break

        sql, params = _build_select(args, after_id=after_id)
        params_with_limit = [*params, fetch_size]
        rows = conn.execute(sql, params_with_limit).fetchall()
        counters["scanned"] += len(rows)

        if not rows:
            break

        after_id = rows[-1][0]  # advance cursor

        updates: list[tuple[str, str]] = []
        for embedding_id, memory_id, content, metadata_json in rows:
            base_text = (content or "").strip()
            if not base_text:
                counters["skipped_empty"] += 1
                continue
            if args.augment_anchors:
                embed_text = mc._augment_embed_text_with_anchors(base_text, metadata_json)
            else:
                embed_text = base_text
            hash_value = mc._content_hash(embed_text)
            updates.append((hash_value, embedding_id))

        if not updates:
            continue

        if args.dry_run:
            counters["updated"] += len(updates)
        else:
            try:
                conn.executemany(update_sql, updates)
                conn.commit()
                counters["updated"] += len(updates)
            except Exception as e:  # noqa: BLE001 — classified below
                # Roll back first: on PostgreSQL a failed statement aborts
                # the transaction and every later batch would fail with it.
                conn.rollback()
                if is_defect(e):
                    raise  # a wrong query fails every batch; stop loudly
                counters["errors"] += 1
                print(
                    f"  WRITE_FAIL: observed: {type(e).__name__}: {e}. "
                    f"inspect: a locked or busy store; this batch is skipped, "
                    f"re-run to retry it",
                    file=sys.stderr,
                )
                # Don't abort; cursor advanced, next batch tries different rows
                continue

        elapsed = time.monotonic() - started
        rate = counters["updated"] / max(elapsed, 1e-3)
        ts = utc_iso()
        print(
            f"[{ts}] scanned={counters['scanned']} "
            f"updated={counters['updated']} "
            f"skipped_empty={counters['skipped_empty']} "
            f"errors={counters['errors']} "
            f"rate={rate:.0f}/s"
        )

    return counters


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sel = ap.add_argument_group("selection")
    sel.add_argument(
        "--db",
        default=None,
        help="Target SQLite DB (default: the active store, or the chatlog "
        "store with --target chatlog). Not used on PostgreSQL.",
    )
    sel.add_argument(
        "--target",
        choices=("core", "chatlog"),
        default="core",
        help="Which store's embeddings to backfill. On PostgreSQL this picks "
        "memory_* or chat_log_* in the one database.",
    )
    sel.add_argument(
        "--type",
        action="append",
        default=None,
        help=f"Memory type to include. Repeatable. "
        f"Defaults to {' + '.join(DEFAULT_TYPES)} (raw-content "
        f"hashing is safe for these). For other types, pass "
        f"--type explicitly with --augment-anchors so the "
        f"hash matches what _embed_many would compute.",
    )
    sel.add_argument(
        "--all-types",
        action="store_true",
        help="Backfill rows of any type (no type filter). Pair "
        "with --augment-anchors so hashes match what "
        "memory_write_impl would have computed at write "
        "time. Note: rows whose metadata.temporal_anchors "
        "predates current schema will get a hash that "
        "doesn't match new writes — strictly an improvement "
        "on NULL (cache miss → cache hit on identical "
        "future text) but may not deduplicate against "
        "older inline-written embeddings of the same text.",
    )
    sel.add_argument(
        "--variant", action="append", default=[], help="Filter to memory_items.variant. Repeatable for OR."
    )
    sel.add_argument("--user-id", type=str, default=None, help="Filter to one memory_items.user_id.")
    sel.add_argument(
        "--id-prefix",
        type=str,
        default=None,
        help="Backfill only embedding rows whose id starts with this hex prefix. Use to shard across instances.",
    )
    sel.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after AT LEAST N successful updates. The check "
        "fires at batch boundaries; actual stop can overshoot "
        "by up to one batch (--batch-size).",
    )

    perf = ap.add_argument_group("performance")
    perf.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per UPDATE batch. Default: {DEFAULT_BATCH_SIZE}.",
    )

    beh = ap.add_argument_group("behavior")
    beh.add_argument(
        "--augment-anchors",
        action="store_true",
        help="Apply memory_core._augment_embed_text_with_anchors "
        "to content before hashing. Required for non-chatlog "
        "types where memory_write_impl applied this transform "
        "at write time. Default OFF (chatlog uses raw content).",
    )
    beh.add_argument("--dry-run", action="store_true", help="Count rows that would be updated; write nothing.")

    args = ap.parse_args(argv)
    if args.all_types:
        if args.type:
            ap.error("--all-types and --type are mutually exclusive")
        args.type = []  # empty list = no type filter
    elif args.type is None:
        args.type = list(DEFAULT_TYPES)
    return args


def _chatlog_default() -> str | None:
    try:
        from chatlog_config import chatlog_db_path
    except ImportError:  # bootstrap: no chatlog layer
        return None
    return chatlog_db_path()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        args.db = resolve_cli_store(args.db, "--db", default=_chatlog_default() if args.target == "chatlog" else None)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    with pointed_store_connection(args.db) as conn:
        return _main(conn, args)


def _main(conn, args: argparse.Namespace) -> int:
    try:
        _verify_schema(conn, args)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    pending = _count_pending(conn, args)
    print(f"DB:        {args.db or seam_backend().name} (target {args.target})")
    print(f"Pending:   {pending}")
    print("Filters:")
    print(f"  type:        {args.type}")
    if args.variant:
        print(f"  variant:     {args.variant}")
    if args.user_id:
        print(f"  user_id:     {args.user_id}")
    if args.id_prefix:
        print(f"  id_prefix:   {args.id_prefix!r}")
    if args.limit:
        print(f"  limit:       {args.limit}")
    print(f"Behavior:  augment_anchors={args.augment_anchors} batch_size={args.batch_size}")

    if args.dry_run:
        print()
        print("(dry-run: no UPDATEs written)")
        return 0

    if pending == 0:
        print()
        print("No NULL-content_hash rows match; nothing to do.")
        return 0

    counters = _run_backfill(conn, args)
    print()
    print("=" * 64)
    print("  backfill_content_hash COMPLETE")
    print("=" * 64)
    print(f"  scanned:        {counters['scanned']}")
    print(f"  updated:        {counters['updated']}")
    print(f"  skipped_empty:  {counters['skipped_empty']}")
    print(f"  errors:         {counters['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
