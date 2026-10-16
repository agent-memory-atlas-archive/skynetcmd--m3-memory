"""Tests for homecoming.py's chatlog db_path rewrite (Project Homecoming fix).

A verbatim copy of .chatlog_config.json leaves its pinned `db_path` pointing at
the OLD location, so the chatlog falls back there even after the DB is migrated.
homecoming._rewrite_chatlog_db_path must repoint it at the new engine root,
preserve other config keys, and be idempotent.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import homecoming  # noqa: E402


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _db(engine_root, name="agent_chatlog.db"):
    os.makedirs(engine_root, exist_ok=True)
    open(os.path.join(engine_root, name), "wb").close()


def test_rewrites_stale_db_path(tmp_path):
    cfg = tmp_path / ".chatlog_config.json"
    _write(cfg, {"db_path": "/old/m3-memory/memory/agent_chatlog.db", "keep": 1})
    engine_root = str(tmp_path / "engine")
    _db(engine_root)

    assert homecoming._rewrite_chatlog_db_path(str(cfg), engine_root) is True

    out = json.loads(cfg.read_text())
    assert out["db_path"] == os.path.join(engine_root, "agent_chatlog.db")
    assert out["keep"] == 1  # other keys preserved


def test_idempotent(tmp_path):
    cfg = tmp_path / ".chatlog_config.json"
    engine_root = str(tmp_path / "engine")
    correct = os.path.join(engine_root, "agent_chatlog.db")
    _write(cfg, {"db_path": correct})

    homecoming._rewrite_chatlog_db_path(str(cfg), engine_root)
    assert json.loads(cfg.read_text())["db_path"] == correct


def test_missing_db_path_key_is_set(tmp_path):
    # A config with no db_path at all gets one pointing at the engine root.
    cfg = tmp_path / ".chatlog_config.json"
    engine_root = str(tmp_path / "engine")
    _write(cfg, {"unrelated": "x"})
    _db(engine_root)

    assert homecoming._rewrite_chatlog_db_path(str(cfg), engine_root) is True
    out = json.loads(cfg.read_text())
    assert out["db_path"] == os.path.join(engine_root, "agent_chatlog.db")
    assert out["unrelated"] == "x"


def test_unreadable_config_does_not_raise(tmp_path):
    # A non-existent / unreadable config must be a quiet no-op, not a crash.
    homecoming._rewrite_chatlog_db_path(str(tmp_path / "nope.json"), str(tmp_path))


def test_malformed_json_does_not_raise(tmp_path):
    cfg = tmp_path / ".chatlog_config.json"
    cfg.write_text("{not valid json")
    homecoming._rewrite_chatlog_db_path(str(cfg), str(tmp_path))  # no exception


def test_a_target_db_that_does_not_exist_is_not_written_and_fails(tmp_path):
    cfg = tmp_path / ".chatlog_config.json"
    _write(cfg, {"db_path": "/old/agent_chatlog.db"})
    assert homecoming._rewrite_chatlog_db_path(str(cfg), str(tmp_path / "engine")) is False
    assert json.loads(cfg.read_text())["db_path"] == "/old/agent_chatlog.db", "a dangling db_path was written"


def test_the_migrated_db_name_is_used_not_a_literal(tmp_path):
    cfg = tmp_path / ".chatlog_config.json"
    engine_root = str(tmp_path / "engine")
    _db(engine_root, "custom_chat.db")
    _write(cfg, {"db_path": "/old/custom_chat.db"})
    assert homecoming._rewrite_chatlog_db_path(str(cfg), engine_root, db_name="custom_chat.db") is True
    assert json.loads(cfg.read_text())["db_path"] == os.path.join(engine_root, "custom_chat.db")


def test_an_unwritable_config_reports_failure(tmp_path, monkeypatch):
    import importlib

    cfg = tmp_path / ".chatlog_config.json"
    engine_root = str(tmp_path / "engine")
    _db(engine_root)
    _write(cfg, {"db_path": "/old/agent_chatlog.db"})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(importlib.import_module("m3_core.atomic_io"), "atomic_write", boom)
    assert homecoming._rewrite_chatlog_db_path(str(cfg), engine_root) is False
    assert json.loads(cfg.read_text())["db_path"] == "/old/agent_chatlog.db"


def _sqlite(path, rows):
    import sqlite3

    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memory_items (id TEXT)")
    conn.executemany("INSERT INTO memory_items VALUES (?)", [(str(i),) for i in range(rows)])
    conn.commit()
    conn.close()


def _run_main(tmp_path, monkeypatch, caplog, src_rows, dst_rows):
    src = str(tmp_path / "legacy" / "agent_memory.db")
    engine = str(tmp_path / "engine")
    _sqlite(src, src_rows)
    _sqlite(os.path.join(engine, "agent_memory.db"), dst_rows)
    monkeypatch.setattr(homecoming, "get_legacy_assets", lambda: {"core_db": (src, "engine")})
    monkeypatch.setattr(homecoming, "get_m3_engine_root", lambda: engine)
    monkeypatch.setattr(homecoming, "get_m3_config_root", lambda: str(tmp_path / "config"))
    monkeypatch.setattr(homecoming, "migrate_memory_root", lambda: {"skipped": "test", "failed": []})
    import logging

    with caplog.at_level(logging.INFO):
        return homecoming.main()


def test_an_existing_destination_with_fewer_rows_fails_the_run(tmp_path, monkeypatch, caplog):
    assert _run_main(tmp_path, monkeypatch, caplog, src_rows=5, dst_rows=2) == 1
    assert "MORE rows" in caplog.text and "memory_items 5 vs 2" in caplog.text


def test_an_existing_destination_with_all_rows_is_kept(tmp_path, monkeypatch, caplog):
    assert _run_main(tmp_path, monkeypatch, caplog, src_rows=2, dst_rows=3) == 0
    assert "core_db" in caplog.text and "Kept existing" in caplog.text
