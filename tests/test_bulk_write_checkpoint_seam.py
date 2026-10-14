"""A bulk write checkpoints through the storage seam (StorageBackend.maintenance_checkpoint).

It ran SQLite's `PRAGMA wal_checkpoint` directly, so on PostgreSQL every bulk write
logged a syntax-error defect (found 2026-10-13 by the PG exact-scan benchmark).
"""

from __future__ import annotations

import asyncio
import importlib
import logging

import pytest


@pytest.fixture
def stub_embedder(monkeypatch):
    dim = importlib.import_module("memory.config").EMBED_DIM
    model = importlib.import_module("memory.config").EMBED_MODEL

    async def _embed_many(texts):
        return [([0.1] * dim, model) for _ in texts]

    for name in ("memory.embed", "memory_core"):
        mod = importlib.import_module(name)
        if hasattr(mod, "_embed_many"):
            monkeypatch.setattr(mod, "_embed_many", _embed_many, raising=False)
    monkeypatch.setattr(importlib.import_module("memory.embed"), "fast_embedder_available", lambda: True)


def test_a_bulk_write_checkpoints_through_the_seam_on_each_backend(each_backend, stub_embedder, caplog):
    write = importlib.import_module("memory.write")
    calls = []
    real = type(each_backend).maintenance_checkpoint

    def _spy(self, conn, *, final=False):
        calls.append(final)
        return real(self, conn, final=final)

    caplog.set_level(logging.WARNING)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(each_backend), "maintenance_checkpoint", _spy)
        ids = asyncio.run(
            write.memory_write_bulk_impl(
                [{"type": "note", "title": "seam", "content": "checkpoint through the seam"}],
                enrich=False,
                check_contradictions=False,
            )
        )
    assert len(ids) == 1
    assert calls == [False], "the bulk write did not checkpoint through StorageBackend.maintenance_checkpoint"
    assert "checkpoint WAL after bulk write" not in caplog.text, caplog.text
