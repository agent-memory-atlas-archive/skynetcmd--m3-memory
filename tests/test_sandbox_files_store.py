"""The per-test sandbox must cover the FILES store, not only the core roots.

memory.config.FILES_DB_PATH is resolved at import, so pinning the engine and
memory roots per test does not move it. Before conftest re-pointed it, an
in-process files_stats call in a test returned the developer's live
files_database.db (measured 2026-10-08: 20 files, 218 leaves). Files tools that
write could have modified that store.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path


def _inside(path: str, root: Path) -> bool:
    return os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(str(root.resolve())))


def test_files_store_resolves_inside_the_test_sandbox(tmp_path):
    resolve = importlib.import_module("files_memory.db")._resolve_path
    assert _inside(resolve(None), tmp_path), f"files store escaped the sandbox: {resolve(None)}"


def test_frozen_files_db_constant_is_repointed(tmp_path):
    for name in ("memory.config", "files_memory.config"):
        value = importlib.import_module(name).FILES_DB_PATH
        assert _inside(value, tmp_path), f"{name}.FILES_DB_PATH escaped the sandbox: {value}"
