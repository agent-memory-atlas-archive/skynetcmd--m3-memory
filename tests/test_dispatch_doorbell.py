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


def _core_bin(base: str) -> str | None:
    """A core-wheel binary, located as m3 locates it (m3_memory._core_bins)."""
    from m3_core.seams import core_bins

    found = core_bins.core_binary(base)
    return str(found) if found else None


@pytest.fixture
def daemon(tmp_path):
    exe = _core_bin("m3-dispatchd")
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


def test_m3_wait_wakes_on_a_python_send_and_runs_the_confirm_command(daemon, stores, monkeypatch):
    """Python ring -> daemon -> m3-wait -> the confirm command, live."""
    from memory import orchestration

    exe = _core_bin("m3-wait")
    if not exe:
        pytest.skip(skip_reason("dep", "m3-wait is not installed (a core with the waiter binary)"))
    monkeypatch.setattr(doorbell, "endpoint_for", lambda root: daemon)
    assert doorbell.wait(daemon, ["probe"], 0)["wake"] == "resync"  # registered: the next wake is mail
    confirm = [
        sys.executable,
        "-c",
        "import json,os,sys; print(json.loads(os.environ['M3_WAKE'])['wake']); sys.exit(0)",
    ]
    proc = subprocess.Popen(
        [exe, "--endpoint", daemon, "--reader", "probe", "--timeout", "20", "--", *confirm],
        stdout=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.3)
    orchestration.notify_impl("probe@7", "handoff", {})
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0 and out.strip() == "mail", (proc.returncode, out)


def test_the_confirm_step_reports_new_mail_once(stores, capsys, tmp_path):
    """What m3-wait runs per wake: exit 0 with the new ids, then 75 (wait again).
    The first run has no baseline, so mail already waiting is reported once."""
    import argparse

    import m3_notification_waiter as w

    from memory import orchestration

    orchestration.notify_impl("probe@2", "handoff", {})  # arrived before any waiter ran
    args = argparse.Namespace(agent_ids=["probe"], ack=False, engine_root=str(tmp_path))
    assert w._confirm(args) == 0
    assert len(json.loads(capsys.readouterr().out.strip().splitlines()[-1])["new_notifications"]["probe"]) == 1
    assert w._confirm(args) == w._KEEP_WAITING
    orchestration.notify_impl("probe@3", "handoff", {})
    assert w._confirm(args) == 0
    assert len(json.loads(capsys.readouterr().out.strip().splitlines()[-1])["new_notifications"]["probe"]) == 1
    assert w._confirm(args) == w._KEEP_WAITING, "the same arrival was reported twice"


def test_every_reader_that_sees_a_message_is_told_once(stores, capsys, tmp_path):
    """A message to probe@4 reaches a bare `probe` waiter AND a `probe@4` waiter.
    Receipt is one stamp per row, so it cannot be the baseline: the second
    waiter would never wake. Each reader set keeps its own."""
    import argparse

    import m3_notification_waiter as w

    from memory import orchestration

    bare = argparse.Namespace(agent_ids=["probe"], ack=False, engine_root=str(tmp_path))
    inst = argparse.Namespace(agent_ids=["probe@4"], ack=False, engine_root=str(tmp_path))
    w._confirm(bare), w._confirm(inst)  # take baselines
    orchestration.notify_impl("probe@4", "handoff", {})
    assert w._confirm(bare) == 0
    assert w._confirm(inst) == 0, "the instance waiter missed mail the bare waiter confirmed first"
    assert w._confirm(bare) == w._KEEP_WAITING, "the bare waiter re-reported: its baseline was overwritten"
    assert w._confirm(inst) == w._KEEP_WAITING, "the instance waiter re-reported: its baseline was overwritten"
    capsys.readouterr()


def test_start_daemon_brings_one_up_and_a_second_start_does_not_double_it(tmp_path):
    """What the waiter does when nothing answers. A racing second start exits
    ("already serving"), leaving the first in place."""
    import psutil

    exe = _core_bin("m3-dispatchd")
    if not exe:
        pytest.skip(skip_reason("dep", "m3-dispatchd is not installed (a core with the dispatch daemon)"))
    endpoint = doorbell.endpoint_for(str(tmp_path / "engine"))
    (tmp_path / "engine" / ".internal").mkdir(parents=True, exist_ok=True)
    log = str(tmp_path / "logs" / "m3-dispatchd.log")
    assert not doorbell.alive(endpoint)
    assert doorbell.start_daemon(endpoint, exe, log)
    try:
        assert doorbell.start_daemon(endpoint, exe, log)  # still answers: the first one
        time.sleep(0.5)
        mine = [p for p in psutil.process_iter(["cmdline"]) if endpoint in " ".join(p.info.get("cmdline") or [])]
        assert len(mine) == 1, f"{len(mine)} daemons on one endpoint"
        assert "already serving" in pathlib.Path(log).read_text(encoding="utf-8", errors="replace")
    finally:
        for p in psutil.process_iter(["cmdline"]):
            if endpoint in " ".join(p.info.get("cmdline") or []):
                p.kill()


def test_status_reports_a_live_daemon_and_none_without_one(daemon, tmp_path):
    assert doorbell.status(doorbell.endpoint_for(str(tmp_path / "nothing-here"))) is None
    doorbell.wait(daemon, ["probe"], 0)
    view = doorbell.status(daemon)
    assert view is not None and "probe" in view["readers"]
