import argparse
import asyncio
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(BASE_DIR, "bin"))

import memory_core  # noqa: F401 — loads the payload's memory package before memory.write
from m3_core.paths import pin_database  # noqa: E402
from m3_sdk import add_database_arg, resolve_db_path


async def re_embed_all(db_path: str):
    print(f"Connecting to {db_path}...")
    from m3_core.paths import active_database, seam_backend

    # A WRITABLE connection to db_path: this rewrites embeddings, and
    # open_readonly is read-only (mode=ro on SQLite), so it could never commit.
    _scope = active_database(str(db_path))
    _scope.__enter__()
    _cm = seam_backend().connection()
    conn = _cm.__enter__()
    try:
        conn.row_factory = sqlite3.Row
    except Exception:  # noqa: BLE001 — non-SQLite connection
        pass

    items = conn.execute("SELECT id, content, title FROM memory_items WHERE is_deleted = 0").fetchall()
    print(f"Found {len(items)} active items to re-embed.")

    # Listing done; release the connection before re-embedding, which opens its
    # own (rewindow_memory writes through memory.db._db, bound by _scope).
    conn.commit()
    _cm.__exit__(None, None, None)

    # rewindow_memory is the write path's own owner: windows as a fresh write
    # stores them, the first 'default'. The former per-item UPDATE wrote ONE vector
    # over every window row, and embedded the text without the write path's anchors.
    from memory.write import rewindow_memory

    updated = failed = 0
    for i, item in enumerate(items, 1):
        rid = item["id"]
        if not (item["content"] or item["title"] or "").strip():
            print(f"[{i}/{len(items)}] Skipping {rid} (no embeddable text)")
            continue
        print(f"[{i}/{len(items)}] Re-embedding {rid}...")
        if await rewindow_memory(rid):
            updated += 1
        else:
            failed += 1
            print(f"FAILED to embed {rid} (its existing embedding rows were kept)")

    _scope.__exit__(None, None, None)
    print(f"Successfully re-embedded {updated} items; {failed} failed.")
    if failed:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description="Re-embed every active memory item.")
    add_database_arg(parser)
    args = parser.parse_args()

    db_path = resolve_db_path(args.database)
    if args.database:
        pin_database(args.database)
    asyncio.run(re_embed_all(db_path))


if __name__ == "__main__":
    main()
