"""memory_write_bulk_impl's *_concurrency arguments are accepted but do not govern
anything (process-wide semaphores do): a caller who sets one is told, once."""

from __future__ import annotations

import importlib
import logging


def test_a_set_concurrency_kwarg_warns_once_and_the_default_is_silent(caplog, monkeypatch):
    write = importlib.import_module("memory.write")
    monkeypatch.setattr(write, "_BULK_CONCURRENCY_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="memory.write"):
        write._warn_ignored_bulk_concurrency(2, 2)
        assert not caplog.records
        write._warn_ignored_bulk_concurrency(8, 2)
        write._warn_ignored_bulk_concurrency(8, 2)
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1 and "fact_enricher_concurrency" in msgs[0]
    assert "observed:" in msgs[0] and "inspect:" in msgs[0]
