import logging
import os
import sys

# Dynamically resolve project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

from m3_sdk import M3Context

ctx = M3Context.for_db(None)
DB_PATH = ctx.db_path

logger = logging.getLogger("session_handoff")

try:
    from mcp_compat import FastMCP
except ImportError:
    logger.error("FastMCP not found. Ensure dependencies are installed in the venv.")
    sys.exit(1)

mcp = FastMCP("Session Manager")

from datetime import UTC, datetime


# session_handoff is a CORE-store table, so it is created/read/written through
# the backend-aware seam (mc._db()). On a PG-primary deployment the old
# sqlite3.connect(DB_PATH) created/wrote a stale SQLite file that the live store
# never sees. The DDL diverges per backend, so the column types come from the
# dialect. Created on first use, not at import (M13 did it at module load): the
# backend is selected after imports, by config, --set or a test.
def _ensure_table():
    import memory_core as mc
    from memory.backends import dialect

    d = dialect()
    with mc._db() as conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS session_handoff ({d.autoincrement_id_column()}, project TEXT, "
            f"summary TEXT, next_steps TEXT, timestamp {d.timestamp_type()} DEFAULT CURRENT_TIMESTAMP)"
        )


@mcp.tool()
def save_handoff(project: str, summary: str, next_steps: str):
    """Saves the current AI session state for another agent to resume."""
    try:
        import memory_core as mc
        from memory.backends import dialect

        _ensure_table()
        _p = dialect().param()
        with mc._db() as conn:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                f"INSERT INTO session_handoff (project, summary, next_steps, timestamp) "
                f"VALUES ({_p}, {_p}, {_p}, {_p})",
                (project, summary, next_steps, now),
            )
        return f"State saved for {project}."
    except Exception as exc:
        return f"Error saving handoff: {type(exc).__name__}"


if __name__ == "__main__":
    mcp.run()
