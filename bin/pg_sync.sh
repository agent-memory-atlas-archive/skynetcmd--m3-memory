#!/bin/bash
set -euo pipefail
# pg_sync.sh — macOS/Linux hourly sync wrapper.
# Delegates to sync_all.py which handles pg_sync and writes its own log under
# the logs root (M3_LOGS_ROOT > M3_MEMORY_ROOT/logs > ~/.m3/logs), never under
# this payload: a reinstall wipes the payload.
# Registered via cron: 0 * * * * /path/to/bin/pg_sync.sh

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"

# The interpreter: M3_PYTHON when set, else a dev checkout's .venv, else python3
# on PATH. A hardcoded .venv path does not exist in a pipx install.
if [ -n "${M3_PYTHON:-}" ]; then
    PYTHON="$M3_PYTHON"
elif [ -x "$WORKSPACE/.venv/bin/python" ]; then
    PYTHON="$WORKSPACE/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "pg_sync.sh: no Python found (checked M3_PYTHON, $WORKSPACE/.venv/bin/python, python3 on PATH). fix: set M3_PYTHON in the cron entry to the interpreter m3 is installed in" >&2
    exit 1
fi

exec "$PYTHON" "$WORKSPACE/bin/sync_all.py"
