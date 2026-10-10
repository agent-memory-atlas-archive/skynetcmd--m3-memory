import argparse
import asyncio
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(BASE_DIR, "bin"))

from m3_sdk import add_database_arg, resolve_db_path
from memory_core import _embed, _pack


async def re_embed_all(db_path: str):
    print(f"Connecting to {db_path}...")
    from m3_core.paths import active_database, seam_backend, seam_dialect
    _p = seam_dialect().param()
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

    updated = 0
    for item in items:
        rid = item['id']
        text = item['content'] or item['title'] or ""

        if not text.strip():
            print(f"[{updated+1}/{len(items)}] Skipping {rid} (no embeddable text)")
            continue

        print(f"[{updated+1}/{len(items)}] Re-embedding {rid}...")
        vec, model = await _embed(text)

        if vec:
            blob = _pack(vec)
            existing = conn.execute(
                f"SELECT id FROM memory_embeddings WHERE memory_id = {_p}", (rid,)
            ).fetchone()
            if existing:
                conn.execute(
                    f"UPDATE memory_embeddings SET embedding = {_p}, embed_model = {_p}, dim = {_p} WHERE memory_id = {_p}",
                    (blob, model, len(vec), rid)
                )
            else:
                import uuid
                conn.execute(
                    f"INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim) VALUES ({seam_dialect().placeholder(5)})",
                    (str(uuid.uuid4()), rid, blob, model, len(vec))
                )
            updated += 1
        else:
            print(f"FAILED to embed {rid}")

    conn.commit()
    _cm.__exit__(None, None, None)
    _scope.__exit__(None, None, None)
    print(f"Successfully re-embedded {updated} items.")


def main():
    parser = argparse.ArgumentParser(description="Re-embed every active memory item.")
    add_database_arg(parser)
    args = parser.parse_args()

    db_path = resolve_db_path(args.database)
    if args.database:
        os.environ["M3_DATABASE"] = args.database
    asyncio.run(re_embed_all(db_path))


if __name__ == "__main__":
    main()
