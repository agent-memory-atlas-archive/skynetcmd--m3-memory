"""memory_graph stays inside the root memory's tenancy.

memory_graph_impl walked memory_relationships with only `is_deleted = 0`, so an
edge from one user's memory listed another user's memory (id, title, type), and
another agent's private one, and walked on through them.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


def _store(tmp_path):
    from conftest import create_full_main_schema

    db = tmp_path / "g.db"
    create_full_main_schema(db)
    items = [
        # id, user_id, agent_id, scope, is_deleted
        ("root", "alice", "agent-x", "agent", 0),
        ("same-user", "alice", "agent-x", "user", 0),
        ("other-user", "bob", "agent-x", "user", 0),
        ("beyond-other-user", "alice", "agent-x", "user", 0),
        ("other-agent-private", "alice", "agent-y", "agent", 0),
        ("deleted", "alice", "agent-x", "user", 1),
    ]
    edges = [
        ("root", "same-user"),
        ("root", "other-user"),
        ("other-user", "beyond-other-user"),
        ("root", "other-agent-private"),
        ("root", "deleted"),
    ]
    with sqlite3.connect(str(db)) as conn:
        for mid, uid, aid, scope, dele in items:
            conn.execute(
                "INSERT INTO memory_items (id, type, title, content, created_at, is_deleted, user_id, agent_id, scope)"
                " VALUES (?, 'note', ?, 'c', '2026-01-01T00:00:00Z', ?, ?, ?, ?)",
                (mid, mid, dele, uid, aid, scope),
            )
        for i, (a, b) in enumerate(edges):
            conn.execute(
                "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type) VALUES (?, ?, ?, 'related')",
                (f"e{i}", a, b),
            )
    return db


def test_graph_lists_and_walks_only_the_roots_tenancy(monkeypatch, tmp_path):
    db = _store(tmp_path)

    @contextmanager
    def fake_db(existing=None, *a, **k):
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    graph = importlib.import_module("memory.graph")
    monkeypatch.setattr(graph, "_db", fake_db)
    out = json.loads(graph.memory_graph_impl("root", depth=3, as_records=True))
    ids = {r["id"] for r in out["items"]}
    assert ids == {"root", "same-user"}, ids
    edge_ends = {(e["from_id"], e["to_id"]) for e in out["edges"]}
    assert edge_ends == {("root", "same-user")}, edge_ends
