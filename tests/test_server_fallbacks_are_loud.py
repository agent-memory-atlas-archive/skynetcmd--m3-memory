"""Server-path fallbacks that used to hide a failure (BLE001 triage, decided).

Each case pins the new behaviour: a failure is classified, raised, recorded or
warned, never silently turned into a default that looks like success.
"""
from __future__ import annotations

import contextlib
import importlib
import logging
import sqlite3

import httpx
import pytest


# ── bulk embed: only network trouble is transient ──────────────────────────────
def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://embed/v1/embeddings")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req, text="body"))


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), _status_error(503),
    _status_error(429), _status_error(413),
])
def test_retryable_failures_are_transient(exc):
    emb = importlib.import_module("memory.embed")
    assert emb._classify_bulk_post_error(exc) is emb._EMBED_TRANSIENT


@pytest.mark.parametrize("exc, text", [
    (_status_error(400), "HTTP 400"),
    (KeyError("data"), "KeyError"),
    (TypeError("'NoneType' object is not subscriptable"), "TypeError"),
])
def test_everything_else_is_permanent_and_keeps_its_message(exc, text):
    emb = importlib.import_module("memory.embed")
    kind, msg = emb._classify_bulk_post_error(exc)
    assert kind is emb._EMBED_PERMANENT
    assert text in msg


# ── schema_version: None only for an absent table ──────────────────────────────
class _BrokenConn:
    def execute(self, *a):
        raise sqlite3.OperationalError("disk I/O error")

    def close(self):
        pass


def test_sqlite_schema_version_raises_on_a_real_failure(monkeypatch):
    sb = importlib.import_module("memory.backends.sqlite_backend")
    backend = sb.SqliteBackend.__new__(sb.SqliteBackend)

    @contextlib.contextmanager
    def broken():
        yield _BrokenConn()

    monkeypatch.setattr(backend, "connection", broken, raising=False)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        backend.schema_version()


# ── gate count / JSON metadata / host agents: warned once ──────────────────────
def test_a_failing_gate_query_warns_once(monkeypatch, caplog):
    db = importlib.import_module("memory.db")

    @contextlib.contextmanager
    def broken():
        yield _BrokenConn()

    monkeypatch.setattr(db, "_db", broken)
    monkeypatch.setattr(db, "_GATE_QUERY_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="memory.db"):
        assert db._gate_count_query("SELECT COUNT(*) FROM t") == 0
        assert db._gate_count_query("SELECT COUNT(*) FROM t") == 0
    assert caplog.text.count("gate count failed") == 1


def test_malformed_json_metadata_is_empty_and_warned_once(monkeypatch, caplog):
    dl = importlib.import_module("memory.backends.dialect")
    monkeypatch.setattr(dl, "_MALFORMED_JSON_WARNED", False)
    d = dl.dialect_for("sqlite")
    with caplog.at_level(logging.WARNING, logger="memory.backends.dialect"):
        assert d.json_column_to_dict("{not json") == {}
        assert d.json_column_to_dict(b"\xff\xfe") == {}
    assert caplog.text.count("could not be read") == 1


def test_unreadable_host_agents_config_keeps_builtins_and_warns(monkeypatch, caplog):
    cc = importlib.import_module("chatlog_core")
    cfg = importlib.import_module("chatlog_config")

    def boom():
        raise ValueError("bad host_agents")

    monkeypatch.setattr(cfg, "resolve_config", boom)
    monkeypatch.setattr(cc, "_HOST_AGENTS_WARNED", False)
    with caplog.at_level(logging.WARNING, logger="chatlog_core"):
        assert cc._valid_host_agents() == cc.VALID_HOST_AGENTS
    assert "host_agents config unreadable" in caplog.text


# ── models dir: one owner ──────────────────────────────────────────────────────
def test_models_dir_has_one_owner(monkeypatch, tmp_path):
    paths = importlib.import_module("m3_core.paths")
    emb = importlib.import_module("memory.embed")
    mf = importlib.import_module("memory.model_fetch")
    monkeypatch.setattr(paths, "get_m3_models_root", lambda: str(tmp_path / "owned"))
    assert emb._m3_models_dir() == tmp_path / "owned"
    assert mf.m3_models_dir() == tmp_path / "owned"
