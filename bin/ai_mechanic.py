import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m3_core.context import pointed_store_connection  # noqa: E402
from m3_core.paths import resolve_cli_store, seam_backend, store_is_a_file  # noqa: E402
from m3_sdk import add_database_arg  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# This tool DROPs and recreates tables — it must never run against the live
# memory store by accident. On SQLite the argparse `--database` flag has no
# default, so callers must pass a path explicitly; on PostgreSQL there is one
# store, and --force is the acknowledgement.

REPAIRED = ("project_decisions", "system_focus")


def _owner_migrations() -> list[str]:
    """The active backend's migration chain, in order: the one owner of the DDL."""
    if store_is_a_file():
        from migrate_memory import MIGRATIONS_DIR

        return sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
    from migrate_pg import _PG_MIGRATIONS_DIR

    return sorted(glob.glob(os.path.join(_PG_MIGRATIONS_DIR, "pg_*.up.sql")))


def owner_ddl(table: str) -> list[str]:
    """The statements that define ``table``, read from its owner migrations.

    The repair used to carry its own SQLite DDL: a second copy of the schema,
    and wrong on PostgreSQL. Collects CREATE TABLE, CREATE INDEX ... ON and
    ALTER TABLE for ``table`` in chain order; down migrations are skipped.
    Raises when the chain does not define the table (§3: no silent no-op).
    """
    pat = re.compile(
        rf"^\s*(CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?{table}\b"
        rf"|CREATE\s+(UNIQUE\s+)?INDEX\b[^;]*\bON\s+{table}\b"
        rf"|ALTER\s+TABLE\s+{table}\b)",
        re.IGNORECASE | re.DOTALL,
    )
    out: list[str] = []
    for path in _owner_migrations():
        if path.endswith(".down.sql"):
            continue
        with open(path, encoding="utf-8") as fh:
            body = "\n".join(ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("--"))
        out.extend(st.strip() for st in body.split(";") if pat.match(st))
    if not any(re.match(r"\s*CREATE\s+TABLE", st, re.IGNORECASE) for st in out):
        raise RuntimeError(
            f"observed: no CREATE TABLE {table} in the {seam_backend().name} migrations. "
            f"inspect: memory/migrations (SQLite) / memory/migrations/postgres"
        )
    return out


def repair_database(db_path: str | None):
    print(f"🛠️  Checking Database Schema at {db_path or seam_backend().name}...")
    try:
        ddl = {t: owner_ddl(t) for t in REPAIRED}
        with pointed_store_connection(db_path) as conn:
            for table in REPAIRED:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
                for stmt in ddl[table]:
                    conn.execute(stmt)
            conn.execute("INSERT INTO system_focus (id, summary) VALUES (1, 'System Repaired & Calibrated')")
        print("✅ Database Schema Aligned.")
    except Exception as e:
        print(f"❌ Database Repair Failed: observed: {type(e).__name__}: {e}. inspect: the store and its migrations")


def check_bridges():
    print("🛠️  Checking MCP Bridges...")
    # memory_bridge is m3's only MCP server. The four others checked here were
    # never part of m3 and were deleted 2026-09-17.
    bridges = ["memory_bridge.py"]
    for bridge in bridges:
        path = os.path.join(BASE_DIR, "bin", bridge)
        if os.path.exists(path):
            print(f"✅ Bridge Found: {bridge}")
        else:
            print(f"⚠️  Missing Bridge: {bridge}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emergency schema repair (DESTRUCTIVE: drops project_decisions and system_focus)."
    )
    add_database_arg(parser)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Required. Confirms you understand this drops tables.",
    )
    args = parser.parse_args(argv)

    if store_is_a_file() and not args.database:
        parser.error("--database is required (this tool will not run against the default DB without an explicit path).")
    if not args.force:
        parser.error("--force is required to acknowledge the destructive DROP TABLE operations.")

    try:
        db_path = resolve_cli_store(args.database, "--database")
    except ValueError as e:
        parser.error(str(e))
    print("🚀 Starting AI-OS Emergency Repair...")
    repair_database(db_path)
    check_bridges()
    print("✨ Repair Cycle Finished. Restart your Pulse dashboard and AI CLI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
