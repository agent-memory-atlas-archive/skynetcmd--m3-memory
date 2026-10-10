"""A swallowed defect is a WARNING; a swallowed expected miss stays DEBUG.

Driven with real driver errors on each backend: the classification is the
database seam's (StorageBackend.is_query_defect), so it is tested where the
driver lives, not with hand-built exception objects.
"""
from __future__ import annotations

import ast
import logging
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}


def _debug_only_swallows(tree: ast.AST) -> list[int]:
    """Broad handlers whose only log call is logger.debug and that do not raise."""
    out = []
    for h in ast.walk(tree):
        if not isinstance(h, ast.ExceptHandler):
            continue
        typ = ast.unparse(h.type) if h.type else "bare"
        if "Exception" not in typ and typ != "bare":
            continue
        body = [n for s in h.body for n in ast.walk(s)]
        levels = {n.func.attr for n in body if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute) and n.func.attr in _LEVELS}
        if levels == {"debug"} and not any(isinstance(n, ast.Raise) for n in body):
            out.append(h.lineno)
    return out


def test_no_broad_handler_swallows_at_debug_only():
    hits = []
    for top in ("bin", "m3_memory"):
        for p in (_ROOT / top).rglob("*.py"):
            if p.name.startswith(("bench_", "benchmark_", "test_")) or "__pycache__" in p.parts:
                continue
            rel = p.relative_to(_ROOT).as_posix()
            hits += [f"{rel}:{ln}" for ln in _debug_only_swallows(ast.parse(p.read_text(encoding="utf-8")))]
    assert not hits, ("observed: a broad except that logs only at DEBUG, so a defect it "
                      "catches is invisible: " + ", ".join(hits)
                      + ". inspect: report it with m3_core.errors.log_swallowed")


def _error_from(backend, sql: str) -> BaseException:
    with backend.connection() as conn:
        try:
            conn.execute(sql)
        except Exception as e:  # noqa: BLE001 -- the error is the test subject
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 -- best-effort reset of an aborted PG transaction
                pass
            return e
    pytest.fail(f"observed: {sql!r} succeeded. inspect: the probe needs SQL this backend rejects")


def test_bad_query_is_a_defect(each_backend):
    assert each_backend.is_query_defect(_error_from(each_backend, "SELEC id FROM memory_items"))
    assert each_backend.is_query_defect(
        _error_from(each_backend, "SELECT no_such_column_x FROM memory_items"))


def test_missing_table_is_not_a_defect(each_backend):
    assert not each_backend.is_query_defect(
        _error_from(each_backend, "SELECT id FROM no_such_table_x"))


def test_non_driver_errors_are_not_query_defects(each_backend):
    assert not each_backend.is_query_defect(ValueError("x"))
    assert not each_backend.is_query_defect(TimeoutError())


def test_log_swallowed_levels(each_backend, caplog):
    from m3_core.errors import log_swallowed

    log = logging.getLogger("m3.test.swallowed")
    with caplog.at_level(logging.DEBUG, logger="m3.test.swallowed"):
        e = _error_from(each_backend, "SELEC 1")
        log_swallowed(log, e, "probe query failed: %s", e)
        log_swallowed(log, TimeoutError("slow"), "probe lookup failed")
        log_swallowed(log, TypeError("bad arg"), f"probe code failed for {'id-1'}")
    levels = {r.getMessage().split(" failed")[0]: r.levelno for r in caplog.records}
    assert levels == {"probe query": logging.WARNING, "probe lookup": logging.DEBUG,
                      "probe code": logging.WARNING}
    warn = next(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "observed:" in warn and "inspect:" in warn


def test_classifying_imports_nothing(each_backend, monkeypatch):
    """is_defect runs inside except handlers on any thread; an import there raced
    the main thread's re-import of memory.backends and deadlocked."""
    import builtins

    from m3_core.errors import is_defect

    err = _error_from(each_backend, "SELEC 1")
    real_import = builtins.__import__

    def _no_import(name, *a, **kw):
        if name.split(".")[0] in ("memory", "m3_core", "m3_memory"):
            raise AssertionError(f"observed: is_defect imported {name!r}. inspect: m3_core/errors.py")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_import)
    try:
        assert is_defect(err) is True
        assert is_defect(TimeoutError()) is False
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import)


def test_a_write_through_a_read_only_connection_is_a_defect(each_backend):
    """Parity: SQLite says "attempt to write a readonly database", PostgreSQL
    raises read_only_sql_transaction; both are a caller writing where it must not."""
    from m3_sdk import resolve_db_path

    with each_backend.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    err = None
    try:
        with each_backend.open_readonly(str(resolve_db_path(None))) as conn:
            conn.execute("CREATE TABLE ro_defect_probe (id INTEGER)")
    except Exception as e:  # noqa: BLE001 -- the error is the test subject
        err = e
    assert err is not None and each_backend.is_query_defect(err), (each_backend.name, err)
