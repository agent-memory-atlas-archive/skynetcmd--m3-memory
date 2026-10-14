"""A port that answers is not proof that our server is running.

A trial install on a shared Linux machine: a second user's `m3 setup` printed
"embedder running on port 8082", "dashboard: running" and "Setup complete —
verified healthy" while both ports were held by the first user's m3, its own
dashboard crash-looped on the taken port, and its own embedder could not bind.
Running now means the listener is one of this account's processes. On Windows
the embedder is one machine-wide LocalSystem service by design, so the embedder
rule applies only where it is a per-user service (Linux, macOS).
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


class _Proc:
    def __init__(self, pid, owned, listens=()):
        self.pid, self._owned, self._listens = pid, owned, listens

    def net_connections(self, kind="tcp"):
        import psutil

        return [SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=p)) for p in self._listens]


class _Plat:
    """port_listener_is_mine over a fixed process list (the seam takes it in)."""

    def __init__(self, plat, procs):
        self._plat, self._procs = plat, procs

    def port_listener_is_mine(self, port):
        return self._plat.port_listener_is_mine(port, iter(self._procs))


@pytest.fixture
def platform(monkeypatch):
    plat = importlib.import_module("m3_memory._platform")
    procs: list = []
    monkeypatch.setattr(plat, "owned_by_current_user", lambda p: p._owned)
    return _Plat(plat, procs), procs


def test_a_listener_of_mine_counts(platform):
    plat, procs = platform
    procs[:] = [_Proc(1, True, (8088,))]
    assert plat.port_listener_is_mine(8088) is True


def test_another_users_listener_does_not(platform):
    plat, procs = platform
    procs[:] = [_Proc(1, False, (8088,)), _Proc(2, True, (9999,))]
    assert plat.port_listener_is_mine(8088) is False


def test_an_unknown_owner_may_be_mine(platform):
    plat, procs = platform
    procs[:] = [_Proc(1, None, (8088,))]
    assert plat.port_listener_is_mine(8088) is True


@pytest.mark.parametrize(("uid", "held", "expected"), [(1000, True, True), (1000, False, False), (None, True, False)])
def test_the_embedder_rule_applies_where_it_is_a_per_user_service(monkeypatch, uid, held, expected):
    plat = importlib.import_module("m3_memory._platform")
    ports = importlib.import_module("m3_memory._ports")
    monkeypatch.setattr(plat, "current_uid", lambda: uid)
    monkeypatch.setattr(ports, "held_by_another_owner", lambda port, host="127.0.0.1": held)
    admin = importlib.import_module("m3_memory.embedder_admin")
    assert admin._port_held_by_someone_else(8082) is expected


def test_nothing_answering_is_never_someone_elses(monkeypatch):
    ports = importlib.import_module("m3_memory._ports")
    monkeypatch.setattr(ports, "_answers", lambda port, host="127.0.0.1": False)
    assert ports.held_by_another_owner(8082) is False
