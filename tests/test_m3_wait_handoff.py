"""The notification waiter hands its wait to m3-wait when it can.

With m3-dispatchd running and m3-wait installed, the wait is held by m3-wait
(about 1 MB committed, against 515 MB for the Python child it replaces) and each
wake is confirmed by `m3_notification_waiter --confirm`. Without either, or when
m3-wait fails, the waiter polls exactly as before.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import m3_notification_waiter as w  # noqa: E402

from conftest import simulate_os  # noqa: E402


def test_executable_name_follows_the_os(monkeypatch):
    from m3_memory._platform import executable_name

    simulate_os(monkeypatch, "Windows")
    assert executable_name("m3-wait") == "m3-wait.exe"
    simulate_os(monkeypatch, "Linux")
    assert executable_name("m3-wait") == "m3-wait"


def test_the_wheel_binary_wins_over_path(monkeypatch, tmp_path):
    from m3_memory import _core_bins
    from m3_memory._platform import executable_name

    (tmp_path / executable_name("m3-wait")).write_bytes(b"")
    spec = types.SimpleNamespace(origin=str(tmp_path / "__init__.py"))
    monkeypatch.setattr(_core_bins.importlib.util, "find_spec", lambda name: spec)
    monkeypatch.setattr(_core_bins.shutil, "which", lambda name: "/elsewhere/m3-wait")
    assert _core_bins.core_binary("m3-wait") == tmp_path / executable_name("m3-wait")
    monkeypatch.setattr(_core_bins.importlib.util, "find_spec", lambda name: None)
    assert _core_bins.core_binary("m3-wait") == pathlib.Path("/elsewhere/m3-wait")


def _args(**kw):
    base = {"agent_ids": ["a", "b@1"], "timeout": 30.0, "ack": True, "engine_root": "/eng", "supervise": False}
    return types.SimpleNamespace(**{**base, **kw})


class _Child:
    def __init__(self, cmd, rc, seen):
        seen["cmd"], seen["stdin"], self.rc = cmd, None, rc

    def wait(self):
        return self.rc


def _popen(rc, seen):
    def make(cmd, **kw):
        child = _Child(cmd, rc, seen)
        seen["stdin"] = kw.get("stdin")
        return child

    return make


def test_the_m3_wait_command_carries_readers_and_the_confirm_step(monkeypatch):
    seen = {}
    monkeypatch.setattr(w.subprocess, "Popen", _popen(0, seen))
    monkeypatch.setattr(w.sys, "argv", ["waiter", "--agent-id", "a", "--config", "/c.toml", "--set=x=1"])
    assert w._wait_via_m3_wait(_args(), "/sock", "/bin/m3-wait") == 0
    cmd = seen["cmd"]
    sep = cmd.index("--")
    own, confirm = cmd[:sep], cmd[sep + 1 :]
    assert own[:6] == ["/bin/m3-wait", "--endpoint", "/sock", "--timeout", "30.0", "--exit-when-stdin-closes"]
    assert own[6:] == ["--reader", "a", "--reader", "b@1"]
    assert seen["stdin"] == subprocess.PIPE, "stdin must stay open: its closing tells m3-wait the waiter died"
    assert confirm[2:5] == ["--confirm", "--engine-root", "/eng"]
    assert confirm[5:] == ["--agent-id", "a", "--agent-id", "b@1", "--ack", "--config", "/c.toml", "--set=x=1"]


@pytest.mark.parametrize("rc,expected", [(0, 0), (2, 2), (3, None), (1, None)])
def test_no_daemon_or_a_failure_falls_back_to_polling(monkeypatch, rc, expected):
    monkeypatch.setattr(w.subprocess, "Popen", _popen(rc, {}))
    warned = []
    monkeypatch.setattr(w, "_warn", warned.append)
    assert w._wait_via_m3_wait(_args(), "/sock", "/bin/m3-wait") == expected
    assert bool(warned) == (rc == 1), "only a real m3-wait failure is reported; no daemon is normal"


def test_main_uses_m3_wait_and_never_imports_the_store(monkeypatch):
    monkeypatch.setattr(w, "_ensure_daemon", lambda ep: True)
    monkeypatch.setattr(w, "_doorbell_endpoint", lambda root: "/sock")
    monkeypatch.setattr(w, "_m3_wait_binary", lambda: "/bin/m3-wait")
    monkeypatch.setattr(w, "_wait_via_m3_wait", lambda args, ep, b: 0)
    monkeypatch.setattr(w, "_has_change_signal", lambda store: pytest.fail("polling path reached"))
    monkeypatch.setattr(w.sys, "argv", ["waiter", "--agent-id", "a"])
    assert w.main() == 0


def test_main_polls_when_m3_wait_cannot_serve(monkeypatch):
    class Polled(Exception):
        pass

    def polled(store):
        raise Polled

    monkeypatch.setattr(w, "_ensure_daemon", lambda ep: True)
    monkeypatch.setattr(w, "_doorbell_endpoint", lambda root: "/sock")
    monkeypatch.setattr(w, "_m3_wait_binary", lambda: "/bin/m3-wait")
    monkeypatch.setattr(w, "_wait_via_m3_wait", lambda args, ep, b: None)
    monkeypatch.setattr(w, "_has_change_signal", polled)
    monkeypatch.setattr(w.sys, "argv", ["waiter", "--agent-id", "a"])
    with pytest.raises(Polled):
        w.main()


def test_the_doorbell_socket_path_always_fits_the_limit(monkeypatch):
    """macOS limits a socket path to 104 bytes. A long TMPDIR (a pytest sandbox:
    125 bytes) made the temp fallback too long and the daemon could not bind."""
    import os
    import tempfile

    from m3_memory import _platform

    simulate_os(monkeypatch, "Darwin")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/private/var/folders/" + "y" * 90)
    ep = _platform.dispatch_endpoint("/engine/" + "z" * 90)
    assert len(ep.encode()) < 100
    assert ep.replace("\\", "/").startswith("/tmp/m3-dispatch-"), ep
    short_root = os.path.abspath("/u/.m3/engine")
    assert _platform.dispatch_endpoint(short_root) == os.path.join(short_root, ".internal", "dispatch.sock")


def test_main_polls_when_no_daemon_can_run(monkeypatch):
    class Polled(Exception):
        pass

    def polled(store):
        raise Polled

    monkeypatch.setattr(w, "_ensure_daemon", lambda ep: False)
    monkeypatch.setattr(w, "_doorbell_endpoint", lambda root: "/sock")
    monkeypatch.setattr(w, "_m3_wait_binary", lambda: "/bin/m3-wait")
    monkeypatch.setattr(w, "_wait_via_m3_wait", lambda args, ep, b: pytest.fail("m3-wait with no daemon"))
    monkeypatch.setattr(w, "_has_change_signal", polled)
    monkeypatch.setattr(w.sys, "argv", ["waiter", "--agent-id", "a"])
    with pytest.raises(Polled):
        w.main()


@pytest.mark.parametrize(
    "alive,binary,started,expected,warns",
    [
        (True, None, None, True, False),  # already up: nothing started
        (False, None, None, False, False),  # this core has no daemon: polling is normal, said nowhere
        (False, "/bin/m3-dispatchd", True, True, False),
        (False, "/bin/m3-dispatchd", False, False, True),  # started but silent: reported
    ],
)
def test_the_waiter_keeps_the_daemon_up(monkeypatch, alive, binary, started, expected, warns):
    from m3_core import doorbell

    monkeypatch.setattr(doorbell, "alive", lambda ep: alive)
    monkeypatch.setattr(w, "_core_binary", lambda base: binary if base == "m3-dispatchd" else None)
    calls = []

    def start(ep, b, log, wait_s=3.0):
        calls.append((ep, b, log))
        return started

    monkeypatch.setattr(doorbell, "start_daemon", start)
    warned = []
    monkeypatch.setattr(w, "_warn", warned.append)
    assert w._ensure_daemon("/sock") is expected
    assert bool(calls) == (started is not None)
    if calls:
        assert calls[0][2].endswith("m3-dispatchd.log")
    assert bool(warned) == warns
