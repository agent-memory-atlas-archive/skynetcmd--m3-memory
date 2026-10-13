#!/usr/bin/env python3
"""Seed a fresh SQLite DB with the full m3-memory schema for test isolation.

Brings the store to the current schema with m3's migration runner
(``migrate_memory.py up``), the same path an install takes, so the result can back
the live MCP server, the CLI scripts and the test suites.

Usage:
    python bin/setup_test_db.py --database memory/_test.db
    python bin/test_memory_bridge.py --database memory/_test.db

Exits non-zero if the migrations fail.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

from m3_sdk import add_database_arg, resolve_db_path  # noqa: E402

MIGRATE = os.path.join(BASE_DIR, "bin", "migrate_memory.py")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_database_arg(parser)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Wipe the target DB file before seeding (default: migrate the existing file).",
    )
    args = parser.parse_args()

    db_path = str(resolve_db_path(args.database))
    if args.force:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    # ONE migration runner: a second one (this file used to executescript every
    # migration itself) cannot skip an already-present column the way the runner
    # does, keeps no schema_versions, and drifts from what users get.
    r = subprocess.run([sys.executable, MIGRATE, "--database", db_path, "up", "--yes", "--target", "main"])
    if r.returncode != 0:
        print(f"migrations failed for {db_path} (exit {r.returncode}); see the runner's output above", file=sys.stderr)
        return 2
    print(f"migrated {db_path} to the current schema")
    return 0


if __name__ == "__main__":
    sys.exit(main())
