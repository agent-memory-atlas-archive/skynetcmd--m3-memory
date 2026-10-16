"""memory_consolidate: a NULL-agent group consolidates its own rows, the preview
counts with the write's predicate, pinned rows and empty/unembeddable summaries
never close sources, and the tool refuses to run without an agent."""

from __future__ import annotations

import importlib
import sqlite3
from contextlib import contextmanager

import pytest


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    # The schema is pre-created: skip the migrator (what M3_SKIP_MIGRATIONS does).
    monkeypatch.setattr(importlib.import_module("memory.db"), "_ensure_sync_tables", lambda *a, **k: None)


@pytest.fixture
def mm(monkeypatch, tmp_path):
    import memory_core
    import memory_maintenance

    from conftest import create_full_main_schema

    db_path = tmp_path / "t.db"
    create_full_main_schema(db_path)

    @contextmanager
    def fake_db(existing=None, *a, **k):
        if existing is not None:
            yield existing
            return
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_maintenance, "_db", fake_db)
    memory_maintenance._test_db_path = db_path
    return memory_maintenance


def _seed(mm, n, *, agent="claude", pinned=0, importance=0.3):
    with sqlite3.connect(str(mm._test_db_path)) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO memory_items (id, type, title, content, agent_id, user_id, created_at, importance, "
                "pinned, is_deleted) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (
                    f"o-{agent}-{pinned}-{i}",
                    "note",
                    f"t{i}",
                    f"c{i}",
                    agent,
                    None,
                    "2026-01-01T00:00:00Z",
                    importance,
                    pinned,
                ),
            )


def _patch_llm(monkeypatch, mm, text="SUMMARY", vec=(0.1, 0.2)):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": text}}]}

    class _Client:
        async def post(self, *a, **k):
            return _Resp()

    async def _best(client, token):
        return ("http://local", "test-model")

    async def _embed(t):
        return (list(vec) if vec else None, "test-embed")

    monkeypatch.setattr(mm, "_get_embed_client", lambda: _Client())
    monkeypatch.setattr(mm, "get_best_llm", _best)
    monkeypatch.setattr(mm, "_embed", _embed)
    monkeypatch.setattr(mm.ctx, "get_secret", lambda *a, **k: "tok")


def _deleted(mm):
    with sqlite3.connect(str(mm._test_db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='note' AND is_deleted=1").fetchone()[0]


@pytest.mark.asyncio
async def test_null_user_group_consolidates_its_rows(monkeypatch, mm):
    _seed(mm, 5)
    _patch_llm(monkeypatch, mm)
    out = await mm.memory_consolidate_impl(type_filter="note", threshold=2)
    assert "Consolidated 3 of 3" in out, out
    assert _deleted(mm) == 3


@pytest.mark.asyncio
async def test_preview_counts_with_the_write_predicate(mm):
    _seed(mm, 5, importance=0.9)
    out = await mm.memory_consolidate_impl(type_filter="note", threshold=2, max_importance=0.5, dry_run=True)
    assert "0 of 5 items would consolidate" in out, out


@pytest.mark.asyncio
async def test_pinned_rows_are_never_candidates(mm):
    _seed(mm, 5, pinned=1)
    out = await mm.memory_consolidate_impl(type_filter="note", threshold=2, dry_run=True)
    assert "No memory groups" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("text,vec", [("   ", (0.1,)), ("SUMMARY", None)])
async def test_empty_or_unembeddable_summary_leaves_sources(monkeypatch, mm, text, vec):
    _seed(mm, 5)
    _patch_llm(monkeypatch, mm, text=text, vec=vec)
    out = await mm.memory_consolidate_impl(type_filter="note", threshold=2)
    assert out.startswith("Error consolidating") and "sources untouched" in out, out
    assert _deleted(mm) == 0


@pytest.mark.asyncio
async def test_tool_requires_an_agent_and_previews_by_default(mm):
    _seed(mm, 5)
    refused = await mm.memory_consolidate_tool_impl(type_filter="note", threshold=2)
    assert refused.startswith("Error: agent_filter is required")
    out = await mm.memory_consolidate_tool_impl(agent_filter="claude", type_filter="note", threshold=2)
    assert out.startswith("DRY RUN") and _deleted(mm) == 0
