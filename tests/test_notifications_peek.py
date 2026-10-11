"""notifications_peek reads an inbox without proving anyone alive.

`notifications_poll` heartbeats the polled id, so an agent checking a PEER's
inbox (did my handoff land?) stamped that peer alive: a faked liveness signal
that a reclaim decision would trust. Peek is the same read through the same
owner with every write left out (multi-agent setup proposal A4, m3 817739e8).
"""

from __future__ import annotations

import json
import uuid

import pytest

_ANCIENT = "2020-01-01T00:00:00Z"


@pytest.fixture
def peer(each_backend):
    from memory.orchestration import agent_register_impl, notifications_ack_all_impl, notify_impl

    agent = f"pytest-peek-{uuid.uuid4().hex[:12]}"
    agent_register_impl(agent, "test")
    notify_impl(agent, "handoff", {"note": "peek probe"}, from_agent="pytest-sender")
    yield each_backend, agent
    notifications_ack_all_impl(agent)
    p = _p()
    with each_backend.connection() as conn:
        conn.execute(f"DELETE FROM agents WHERE agent_id = {p}", (agent,))
        conn.commit()


def _p() -> str:
    from memory.backends import dialect

    return dialect().param()


def _last_seen(backend, agent: str):
    with backend.connection() as conn:
        row = conn.execute(f"SELECT last_seen FROM agents WHERE agent_id = {_p()}", (agent,)).fetchone()
    return row["last_seen"]


def _backdate(backend, agent: str) -> None:
    with backend.connection() as conn:
        conn.execute(f"UPDATE agents SET last_seen = {_p()} WHERE agent_id = {_p()}", (_ANCIENT, agent))
        conn.commit()


def test_peek_returns_what_poll_returns(peer):
    from memory.orchestration import notifications_peek_impl, notifications_poll_impl

    backend, agent = peer
    peeked = json.loads(notifications_peek_impl(agent, as_records=True))
    polled = json.loads(notifications_poll_impl(agent, as_records=True))
    assert peeked["items"], f"observed: peek saw no mail on {backend.name}"
    assert [r["id"] for r in peeked["items"]] == [r["id"] for r in polled["items"]], backend.name


def test_peek_leaves_the_peer_looking_exactly_as_alive_as_before(peer):
    from memory.orchestration import notifications_peek_impl, notifications_poll_impl

    backend, agent = peer
    _backdate(backend, agent)
    # Compare the column with itself: PG returns a timestamptz, SQLite the string.
    before = _last_seen(backend, agent)
    out = notifications_peek_impl(agent)
    assert _last_seen(backend, agent) == before, f"observed: peek stamped a heartbeat on {backend.name}"
    assert "read-only" in out and "not registered" not in out, out
    # The other polarity: poll still heartbeats, so the probe above can tell.
    notifications_poll_impl(agent)
    assert _last_seen(backend, agent) != before, "poll stopped heartbeating: the peek test proves nothing"


def test_peek_reports_that_it_recorded_no_heartbeat(peer):
    from memory.orchestration import notifications_peek_impl

    _, agent = peer
    assert json.loads(notifications_peek_impl(agent, as_records=True))["heartbeat_recorded"] is False


def test_peek_refuses_an_empty_agent_id():
    from memory.orchestration import notifications_peek_impl

    with pytest.raises(Exception, match="notifications_peek"):
        notifications_peek_impl("")
