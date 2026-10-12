"""Repo-root conftest: ensure no test ever writes to the production DBs.

The bin/ test scripts (e.g., bin/test_bulk_parity.py) historically did not
patch DB_PATH and leaked fixture rows into memory/agent_memory.db. This
fixture forces the engine root (so the default main store), CHATLOG_DB_PATH
and memory_core.DB_PATH to a per-test tmp dir for every test that runs.
tests/conftest.py's sandbox must not clear what this sets.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
_BIN = REPO_ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

# Files at the repo root that pytest must NOT try to collect.
# CLAUDE.md and GEMINI.md were once tracked as git symlinks (mode
# 120000) pointing at docs/AGENT_INSTRUCTIONS.md so coding agents pick
# up the instructions. They've been converted to regular files in
# 2026-05-01 to fix Windows pytest collection (broken symlinks tripped
# scandir+is_file with OSError WinError 123). Belt-and-braces: keep
# them out of pytest collection regardless of how they're stored.
collect_ignore = ["CLAUDE.md", "GEMINI.md"]


@pytest.fixture(autouse=True)
def _isolate_db_paths(tmp_path, monkeypatch):
    """Force every test to use a tmp DB. Belt-and-braces against tests
    that import memory_core (which captures DB_PATH at import time)
    AND tests that read the env vars on each call."""
    engine = tmp_path / "engine"  # the same root tests/conftest.py's sandbox pins
    tmp_main = engine / "agent_memory.db"
    tmp_chatlog = tmp_path / "test_agent_chatlog.db"
    # The main store as the DEFAULT (the engine root), the lowest rank, so a test
    # that sets its own -- pin_database, or the legacy M3_DATABASE -- still wins;
    # a pin here would outrank them. A developer's exported M3_DATABASE is
    # cleared: it would point a test at a real store.
    monkeypatch.delenv("M3_DATABASE", raising=False)
    monkeypatch.setenv("M3_ENGINE_ROOT", str(engine))
    # Under the name chatlog tests clear to exercise their own resolution; moving
    # it waits for the chatlog-path family's own no-env step.
    monkeypatch.setenv("CHATLOG_DB_PATH", str(tmp_chatlog))
    # Also patch memory_core.DB_PATH if already imported, so tests
    # that captured the constant at import time still get the tmp path.
    if "memory_core" in sys.modules:
        monkeypatch.setattr("memory_core.DB_PATH", str(tmp_main), raising=False)
    yield
