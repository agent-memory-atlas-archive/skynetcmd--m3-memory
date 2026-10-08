"""Pins team membership: touched within 24h, OR client pid alive on its node.

User rule (m3 c6aebe9e): an agent that has not shown activity for 24h and whose
client process is gone has left the team. `status` only records what an agent
last claimed; on 2026-10-07 the live registry showed 12 agents all `active`,
some last seen in April. Membership is derived, so a session that ended without
saying so still reads as gone. Runs against a real SQLite store; no patching of
the code under test (probes are injected).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from m3_core.paths import node_id  # noqa: E402
from m3_core.process import proc_create_time  # noqa: E402
from memory.backends import dialect  # noqa: E402
from memory.orchestration import (  # noqa: E402
    TEAM_MEMBERSHIP_WINDOW_HOURS,
    _db,
    _touch,
    agent_heartbeat_impl,
    agent_list_impl,
    agent_register_impl,
)

_ANCIENT = "2020-01-01T00:00:00Z"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    from conftest import create_full_main_schema
    db_path = tmp_path / "membership.db"
    create_full_main_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    aid = f"pytest-member-{uuid.uuid4().hex[:12]}"
    agent_register_impl(aid, "test")
    return aid


def _row(aid):
    with _db() as db:
        p = dialect().param()
        return db.execute(f"SELECT * FROM agents WHERE agent_id = {p}", (aid,)).fetchone()


def _set(aid, **cols):
    p = dialect().param()
    sets = ", ".join(f"{k} = {p}" for k in cols)
    with _db() as db:
        db.execute(f"UPDATE agents SET {sets} WHERE agent_id = {p}", (*cols.values(), aid))


def _membership(aid):
    rec = next(r for r in json.loads(agent_list_impl(as_records=True))["items"]
               if r["agent_id"] == aid)
    return rec["membership"], rec["membership_basis"]


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_register_stamps_node_hostname_and_utc_db_clock(agent):
    row = _row(agent)
    assert row["node_id"] == node_id()
    assert row["hostname"]
    assert row["last_seen"].endswith("Z") and "T" in row["last_seen"], (
        f"last_seen must be ISO 8601 UTC from the DB clock, got {row['last_seen']!r}")


def test_fresh_touch_is_a_member(agent):
    assert _membership(agent)[0] == "member"


def test_stale_with_live_client_on_this_node_is_idle(agent):
    me = os.getpid()
    _set(agent, last_seen=_ANCIENT, node_id=node_id(), pid=me,
         pid_create_time=proc_create_time(me))
    state, basis = _membership(agent)
    assert state == "idle" and str(me) in basis


def test_stale_with_dead_client_has_left(agent):
    dead = _dead_pid()
    _set(agent, last_seen=_ANCIENT, node_id=node_id(), pid=dead, pid_create_time=None)
    state, basis = _membership(agent)
    assert state == "left" and "not running" in basis


def test_stale_with_reused_pid_has_left(agent):
    """A live pid whose create_time does not match is a DIFFERENT process."""
    me = os.getpid()
    ct = proc_create_time(me)
    if ct is None:
        pytest.skip("psutil unavailable: create_time unreadable")
    _set(agent, last_seen=_ANCIENT, node_id=node_id(), pid=me, pid_create_time=ct - 10)
    assert _membership(agent)[0] == "left"


def test_stale_on_another_node_is_judged_by_time_alone(agent):
    """Its pid cannot be checked from here — even a pid that IS alive locally."""
    me = os.getpid()
    _set(agent, last_seen=_ANCIENT, node_id="SOME-OTHER-NODE", hostname="other-box",
         pid=me, pid_create_time=proc_create_time(me))
    state, basis = _membership(agent)
    assert state == "left" and "other-box" in basis


def test_unresolvable_client_clears_a_stale_pid(agent):
    _set(agent, pid=12345, pid_create_time=1.0)
    with _db() as db:
        _touch(db, agent, client=None)
    row = _row(agent)
    assert row["pid"] is None and row["pid_create_time"] is None


def test_heartbeat_touches_and_reports_db_time(agent):
    _set(agent, last_seen=_ANCIENT)
    out = agent_heartbeat_impl(agent)
    assert out.startswith(f"Heartbeat: {agent}") and _ANCIENT not in out
    assert _membership(agent)[0] == "member"


def test_heartbeat_of_unregistered_agent_is_an_error(agent):
    assert agent_heartbeat_impl("pytest-nobody-" + uuid.uuid4().hex).startswith("Error")


def test_window_is_the_users_24_hours():
    assert TEAM_MEMBERSHIP_WINDOW_HOURS == 24
