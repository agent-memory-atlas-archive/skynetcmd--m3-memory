"""The dispatch doorbell (m3-dispatchd): writers ring after commit, waiters block.

The daemon only says WHEN to look; messages stay rows written and read through
memory.orchestration. So the properties pinned here are about the seams around
it: the ring follows the commit, a missing or faulty daemon costs nothing, the
waiter keeps its confirmation step, and the daemon's addressing agrees with
Python's SQL rule (live, when the binary is available).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from m3_core import doorbell  # noqa: E402
from m3_core import paths as P  # noqa: E402
from m3_core.paths import pin_database  # noqa: E402

from conftest import skip_reason  # noqa: E402

_MAIN_DDL = """
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, kind TEXT,
    payload_json TEXT, created_at TEXT, read_at TEXT, received_at TEXT,
    claimed_by TEXT, claimed_at TEXT, claim_expires_at TEXT, lease_token TEXT,
    attempt_count INTEGER DEFAULT 0, failed_at TEXT);
CREATE TABLE agents (agent_id TEXT PRIMARY KEY, last_seen TEXT, status TEXT);
"""


@pytest.fixture
def stores():
    """Both stores bootstrapped in this test's sandboxed engine root."""
    main = P.resolve_engine_file("agent_memory.db")
    pathlib.Path(main).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(main) as c:
        c.executescript(_MAIN_DDL)
    boot = (pathlib.Path(P.dispatch_migrations_dir()) / "001_bootstrap.up.sql").read_text(encoding="utf-8")
    dispatch = P.resolve_dispatch_db()
    with sqlite3.connect(dispatch) as d:
        d.executescript(boot)
    pin_database(main)
    yield main, dispatch
    pin_database(None)


def test_the_ring_follows_the_commit(stores, monkeypatch):
    """A ring for a row that could still roll back would wake a reader to an
    empty inbox, so the row must already be visible when the ring fires."""
    from memory import orchestration

    _main, dispatch = stores
    seen = []

    def fake_ring(endpoint, recipients):
        with sqlite3.connect(dispatch) as c:
            seen.append((recipients, c.execute("SELECT COUNT(*) FROM notification_dispatch").fetchone()[0]))
        return True

    monkeypatch.setattr(doorbell, "ring", fake_ring)
    orchestration.notify_impl("probe@1", "handoff", {"x": 1})
    assert seen == [(["probe@1"], 1)]


def test_a_doorbell_fault_never_fails_a_send(stores, monkeypatch):
    from memory import orchestration

    def boom(endpoint, recipients):
        raise RuntimeError("daemon exploded")

    monkeypatch.setattr(doorbell, "ring", boom)
    assert "Notified probe@1" in orchestration.notify_impl("probe@1", "handoff", {})


def test_with_no_daemon_a_ring_is_fast_and_false(tmp_path):
    endpoint = doorbell.endpoint_for(str(tmp_path / "engine"))
    t = time.perf_counter()
    assert doorbell.ring(endpoint, ["a"]) is False
    assert time.perf_counter() - t < doorbell.RING_BUDGET_S + 0.2
    assert doorbell.wait(endpoint, ["a"], 0.1) is None


def test_the_waiter_polls_when_there_is_no_daemon():
    import m3_notification_waiter as w

    assert w._doorbell_wait(None, ["a"], 1.0) is None


def _dispatchd() -> str | None:
    """The m3-dispatchd binary: on PATH, or shipped in the m3_core_rs package."""
    found = shutil.which("m3-dispatchd")
    if found:
        return found
    try:
        import m3_core_rs

        pkg = pathlib.Path(m3_core_rs.__file__).parent
    except ImportError:
        return None
    for name in ("m3-dispatchd", "m3-dispatchd.exe"):
        if (pkg / name).is_file():
            return str(pkg / name)
    return None


@pytest.fixture
def daemon(tmp_path):
    exe = _dispatchd()
    if not exe:
        pytest.skip(skip_reason("dep", "m3-dispatchd is not installed (a core with the dispatch daemon)"))
    endpoint = doorbell.endpoint_for(str(tmp_path / "engine"))
    (tmp_path / "engine" / ".internal").mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([exe, "--endpoint", endpoint, "--coalesce-ms", "50"], stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if doorbell.wait(endpoint, ["__probe__"], 0) is not None:
                break
            time.sleep(0.05)
        else:
            pytest.fail("m3-dispatchd did not come up")
        yield endpoint
    finally:
        proc.terminate()
        proc.wait(10)


def test_the_daemon_wakes_exactly_the_readers_python_would_show(daemon):
    """Parity: the daemon's addressing against Python's SQL predicate, over a
    matrix of reader and recipient ids, edge cases included."""
    from memory.orchestration import _addressing_predicate

    ids = ["a", "a@1", "a@2", "a@", "a@@x", "ab@1", "a@1@z", "b"]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (agent_id TEXT)")
    for reader in ids:
        frag, params = _addressing_predicate(reader, "?")
        assert doorbell.wait(daemon, [reader], 0)["wake"] == "resync"  # register
        for recipient in ids:
            conn.execute("DELETE FROM t")
            conn.execute("INSERT INTO t VALUES (?)", (recipient,))
            python_sees = conn.execute(f"SELECT COUNT(*) FROM t WHERE {frag}", params).fetchone()[0] == 1
            doorbell.ring(daemon, [recipient])
            woke = doorbell.wait(daemon, [reader], 0.3)["wake"] == "mail"
            assert woke == python_sees, f"reader {reader!r}, recipient {recipient!r}: daemon {woke}, SQL {python_sees}"


def test_notify_wakes_a_blocked_waiter(daemon, stores, monkeypatch):
    from memory import orchestration

    monkeypatch.setattr(doorbell, "endpoint_for", lambda root: daemon)
    assert doorbell.wait(daemon, ["probe"], 0)["wake"] == "resync"
    got = {}
    th = threading.Thread(target=lambda: got.update(w=doorbell.wait(daemon, ["probe"], 10)))
    th.start()
    time.sleep(0.2)
    t = time.perf_counter()
    orchestration.notify_impl("probe@7", "handoff", {})
    th.join(15)
    assert got["w"]["wake"] == "mail" and got["w"]["recipients"] == ["probe@7"], json.dumps(got)
    assert time.perf_counter() - t < 2.0
