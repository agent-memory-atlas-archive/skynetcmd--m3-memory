"""The cognitive loop's warehouse-sync pass warns when the sync fails (it once
failed hourly for 10 hours with only an INFO summary)."""

from __future__ import annotations

import asyncio
import logging
import types

import pytest


@pytest.fixture
def loop(monkeypatch):
    import m3_cognitive_loop as cl
    import sync_all

    monkeypatch.setattr(cl, "_due", lambda *a: True)
    monkeypatch.setattr(cl, "_record_pass_run", lambda *a: None)
    monkeypatch.setattr(sync_all, "warehouse_target", lambda: ("192.0.2.1", 5432, "test"))
    monkeypatch.setattr(sync_all, "is_reachable", lambda h, p: True)
    return cl, sync_all


@pytest.mark.parametrize("ok, level", [(True, logging.INFO), (False, logging.WARNING)])
def test_the_pass_summary_follows_the_sync_result(loop, monkeypatch, caplog, ok, level):
    cl, sync_all = loop
    monkeypatch.setattr(sync_all, "run_pg_sync", lambda dry: ok)
    with caplog.at_level(logging.INFO, logger=cl.logger.name):
        asyncio.run(cl.run_sync_pass(types.SimpleNamespace(sync_min_interval_s=0)))
    summary = [
        r for r in caplog.records if "Warehouse-sync pass" in r.getMessage() and "Starting" not in r.getMessage()
    ]
    assert [r.levelno for r in summary] == [level]
