"""A path under an untraversable directory reads as "not there", never a crash.

pathlib's is_file() raises PermissionError on Python 3.12/3.13 for such a path
(POSIX); os.path.isfile returns False on every version (m3_memory/_pyver.py).
Pinned on a converted site: _pg_tool must report its own "is not a file".
"""

from __future__ import annotations

import importlib
import os

import pytest

from conftest import skip_reason


@pytest.mark.requires_posix
def test_an_untraversable_tool_path_is_reported_not_raised(tmp_path, m3_settings):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "pg_dump").write_text("#!/bin/sh\n", encoding="utf-8")
    locked.chmod(0)
    try:
        if os.access(locked, os.X_OK):
            pytest.skip(skip_reason("env", "this user can traverse a mode-000 directory (root); nothing to test"))
        pg = importlib.import_module("memory.backends.postgres_backend")
        base = importlib.import_module("memory.backends.base")
        m3_settings({"db.postgres.pg_dump": locked / "pg_dump"})
        with pytest.raises(base.SnapshotError, match="is not a file"):
            pg._pg_tool("pg_dump")
    finally:
        locked.chmod(0o700)
