"""The waiter's baseline and receipt rules, exercised (not grepped).

* A failed or partial inbox read must not empty the baseline: the next good
  read would report the whole inbox as new, and ``--ack`` would mark it read.
* Receipt is reported only after it was checked to be stamped.
* The partial-read signal comes from ``memory.orchestration``'s ``*_checked``
  primitives, which name every store they skipped.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import pathlib
import subprocess
import types
from contextlib import contextmanager

import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "bin" / "m3_notification_waiter.py"


@pytest.fixture
def waiter(monkeypatch):
    spec = importlib.util.spec_from_file_location("m3_notification_waiter_t", _SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_log_path", lambda: None)  # never the real logs root
    return mod


# --- _advance: the single baseline rule ----------------------------------------


def test_failed_read_keeps_the_baseline_and_reports_nothing(waiter):
    gained, nxt = waiter._advance({"1", "2"}, None)
    assert gained == set()
    assert nxt == {"1", "2"}


def test_partial_read_keeps_unseen_ids_in_the_baseline(waiter):
    # store B was skipped: id 2 (in B) is absent but not gone.
    gained, nxt = waiter._advance({"1", "2"}, ({"1", "3"}, True))
    assert gained == {"3"}
    assert nxt == {"1", "2", "3"}
    # B comes back: 2 must NOT be reported as new.
    gained, nxt = waiter._advance(nxt, ({"1", "2", "3"}, False))
    assert gained == set()
    assert nxt == {"1", "2", "3"}


def test_complete_read_replaces_the_baseline(waiter):
    gained, nxt = waiter._advance({"1", "2"}, ({"2", "4"}, False))
    assert gained == {"4"}
    assert nxt == {"2", "4"}


def test_unbaselined_inbox_takes_its_first_good_read_as_baseline(waiter):
    gained, nxt = waiter._advance(None, ({"1", "2"}, False))
    assert gained == set(), "a first read after a failed baseline must not flag the whole inbox"
    assert nxt == {"1", "2"}


def test_unread_ids_returns_none_not_empty_on_failure(waiter, monkeypatch, capsys):
    orch = importlib.import_module("memory.orchestration")

    def boom(agent_id):
        raise RuntimeError("store locked")

    monkeypatch.setattr(orch, "notifications_unread_ids_checked", boom)
    assert waiter.unread_ids("agent-x") is None
    err = capsys.readouterr().err
    assert "detection is BLIND" in err and "agent-x" in err and "store locked" in err


def test_wait_once_does_not_flood_after_a_failed_first_read(waiter, monkeypatch, capsys):
    """Performs the loop: first read fails, second is the baseline, third gains id 3."""
    reads = iter([None, ({"1", "2"}, False), ({"1", "2", "3"}, False)])
    monkeypatch.setattr(waiter, "unread_ids", lambda a: next(reads))
    tokens = iter(range(100))
    monkeypatch.setattr(waiter, "change_token", lambda stores: next(tokens))
    monkeypatch.setattr(waiter, "notification_stores", lambda root: ["s"])
    monkeypatch.setattr(waiter, "_doorbell_endpoint", lambda root: None)
    admin_calls = []
    monkeypatch.setattr(waiter, "_m3_admin", lambda tool, a: admin_calls.append((tool, a)) or True)
    args = types.SimpleNamespace(agent_ids=["a"], engine_root="unused", interval=0.0, timeout=30.0, ack=False)
    assert waiter._wait_once(args) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["new_notifications"] == {"a": ["3"]}
    assert out["received"] == {"a": True}


# --- receipt is reported only when checked --------------------------------------


def _completed(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_admin_requires_the_tools_result_line(waiter, monkeypatch, capsys):
    monkeypatch.setattr(
        waiter.subprocess,
        "run",
        lambda *a, **k: _completed(stdout=json.dumps("Marked 2 notifications received for a")),
    )
    assert waiter._m3_admin("notifications_mark_received", "a") is True

    monkeypatch.setattr(waiter.subprocess, "run", lambda *a, **k: _completed(stdout=""))
    assert waiter._m3_admin("notifications_mark_received", "a") is False
    assert "did not report its result" in capsys.readouterr().err


def test_admin_partial_stamp_is_a_failure(waiter, monkeypatch, capsys):
    text = "Marked 1 notifications received for a; NOT stamped in: table=notification_dispatch"
    monkeypatch.setattr(waiter.subprocess, "run", lambda *a, **k: _completed(stdout=json.dumps(text)))
    assert waiter._m3_admin("notifications_mark_received", "a") is False
    assert "PARTIAL" in capsys.readouterr().err


def test_stamp_receipt_reports_partial_as_not_received(waiter, monkeypatch, capsys):
    orch = importlib.import_module("memory.orchestration")
    monkeypatch.setattr(orch, "notifications_mark_received_checked", lambda a: (1, ["table=x"]))
    assert waiter._stamp_receipt("a") is False
    monkeypatch.setattr(orch, "notifications_mark_received_checked", lambda a: (0, []))
    assert waiter._stamp_receipt("a") is True, "0 rows is not failure: another watcher may have stamped first"


def test_confirm_keeps_a_skipped_stores_ids_in_the_saved_baseline(waiter, monkeypatch, tmp_path, capsys):
    orch = importlib.import_module("memory.orchestration")
    args = types.SimpleNamespace(agent_ids=["a"], engine_root=str(tmp_path), ack=False)
    path = waiter._baseline_path(str(tmp_path), ["a"])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"a": ["5"]}), encoding="utf-8")
    monkeypatch.setattr(orch, "notifications_unread_ids_checked", lambda a: ([1], ["store=x"]))
    monkeypatch.setattr(orch, "notifications_mark_received_checked", lambda a: (1, []))
    assert waiter._confirm(args) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": ["1", "5"]}
    out = json.loads(capsys.readouterr().out.strip())
    assert out["new_notifications"] == {"a": ["1"]}
    assert out["received"] == {"a": True}


def test_no_window_gates_on_the_capability(waiter, monkeypatch):
    monkeypatch.delattr(waiter.subprocess, "STARTUPINFO", raising=False)
    assert waiter._no_window() == {}


# --- orchestration: partial reads are flagged -----------------------------------


@contextmanager
def _conn_returning(rows):
    class _Cur:
        rowcount = 2

        def fetchall(self):
            return rows

    class _Conn:
        def execute(self, *a, **k):
            return _Cur()

    yield _Conn()


def test_read_names_a_skipped_store_and_the_agent(monkeypatch, caplog):
    orch = importlib.import_module("memory.orchestration")
    mb = importlib.import_module("memory.backends")
    monkeypatch.setattr(
        orch, "_notification_sources_checked", lambda: ([(None, "notifications"), ("d.db", "dispatch")], None)
    )
    monkeypatch.setattr(orch, "_db", lambda: _conn_returning([(7,)]))

    class _Backend:
        def open_readonly(self, store):
            raise OSError("database is locked")

    monkeypatch.setattr(mb, "active_backend", lambda: _Backend())
    with caplog.at_level(logging.WARNING, logger="memory_core"):
        rows, skipped = orch.read_across_notification_stores_checked("SELECT id FROM {table}", (), agent_id="ag-1")
    assert rows == [(7,)]
    assert skipped == ["store=d.db table=dispatch"]
    assert "agent_id=ag-1" in caplog.text and "PARTIAL" in caplog.text


def test_unresolved_dispatch_config_is_partial_and_warned_once(monkeypatch, caplog):
    orch = importlib.import_module("memory.orchestration")
    paths = importlib.import_module("m3_core.paths")
    monkeypatch.setattr(orch, "_DISPATCH_CONFIG_REPORTED", set())

    def bad_mode():
        raise ValueError("bad dispatch mode")

    monkeypatch.setattr(paths, "dispatch_store_mode", bad_mode)
    with caplog.at_level(logging.WARNING, logger="memory_core"):
        s1, e1 = orch._notification_sources_checked()
        _s2, e2 = orch._notification_sources_checked()
    assert s1 == [(None, "notifications")] and e1 and "bad dispatch mode" in e1
    assert e2 == e1, "the second call still reports the gap to its caller"
    assert caplog.text.count("dispatch notification store not resolved") == 1, "must not repeat per poll"
    assert orch._notification_sources() == [(None, "notifications")], "the main store is never hidden"


def test_mark_received_checked_reports_skipped_stores(monkeypatch):
    orch = importlib.import_module("memory.orchestration")
    monkeypatch.setattr(orch, "_notification_sources_checked", lambda: ([(None, "notifications")], "ValueError: x"))
    monkeypatch.setattr(orch, "_db", lambda: _conn_returning([]))
    n, skipped = orch.notifications_mark_received_checked("ag-1")
    assert n == 2
    assert skipped == ["dispatch store (unresolved: ValueError: x)"]
    text = orch.notifications_mark_received_impl("ag-1")
    assert text.startswith("Marked 2 notifications received for ag-1") and "NOT stamped in" in text
