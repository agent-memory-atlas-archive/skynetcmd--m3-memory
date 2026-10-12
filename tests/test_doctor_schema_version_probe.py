"""The doctor counts the schema version table the migrator actually writes.

It counted only the legacy `schema_migrations`, which a current store does not
have, so the field was always None and a bare except hid why.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import sqlite3

import pytest

from conftest import use_backend


def _probe(monkeypatch, tables: list[str]):
    doctor = importlib.import_module("memory.doctor")
    db_mod = importlib.import_module("memory.db")
    m3_sdk = importlib.import_module("m3_sdk")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memory_items (id TEXT)")
    for t in tables:
        conn.execute(f"CREATE TABLE {t} (version TEXT)")
        conn.executemany(f"INSERT INTO {t} VALUES (?)", [("1",), ("2",), ("3",)])

    @contextlib.contextmanager
    def fake_db():
        yield conn

    use_backend("sqlite")
    monkeypatch.setattr(db_mod, "_db", fake_db)
    monkeypatch.setattr(m3_sdk, "resolve_db_path", lambda _=None: ":memory:")
    return doctor._probe_db()


@pytest.mark.parametrize("table", ["schema_versions", "schema_migrations"])
def test_counts_whichever_version_table_exists(monkeypatch, caplog, table):
    with caplog.at_level(logging.WARNING, logger="memory.doctor"):
        res = _probe(monkeypatch, [table])
    assert res["status"] == "online"
    assert res["schema_migrations"] == 3
    assert "schema version" not in caplog.text


def test_no_version_table_is_reported(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="memory.doctor"):
        res = _probe(monkeypatch, [])
    assert res["schema_migrations"] is None
    assert "no schema version table" in caplog.text
