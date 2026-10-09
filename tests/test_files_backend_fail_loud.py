"""The files store must not guess its backend or its migration ledger.

`_is_postgres()` answered False whenever the backend could not be resolved, so
a PostgreSQL install's files store went to a separate SQLite file. And
`_applied_versions()` answered "nothing applied" on any error, so the runner
re-applied every migration. Both now raise.
"""
from __future__ import annotations

import importlib

import pytest


def test_an_unresolvable_backend_raises(monkeypatch):
    fdb = importlib.import_module("files_memory.db")
    mb = importlib.import_module("memory.backends")

    def boom():
        raise RuntimeError("backend resolution failed")

    monkeypatch.setattr(mb, "active_backend", boom)
    with pytest.raises(RuntimeError, match="backend resolution failed"):
        fdb._is_postgres()


def test_migrate_uses_the_same_owner():
    fdb = importlib.import_module("files_memory.db")
    fmig = importlib.import_module("files_memory.migrate")
    assert fmig._is_postgres is fdb._is_postgres


class _LedgerUnreadable:
    def execute(self, sql, *a):
        raise RuntimeError("current transaction is aborted")


def test_an_unreadable_ledger_raises_instead_of_reapplying():
    fmig = importlib.import_module("files_memory.migrate")
    with pytest.raises(RuntimeError, match="aborted"):
        fmig._applied_versions(_LedgerUnreadable())
