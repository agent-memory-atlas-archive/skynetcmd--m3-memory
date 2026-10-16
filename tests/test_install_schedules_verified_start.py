"""The installer reports a service as started only after checking it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))

import install_schedules as isch  # noqa: E402


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _scripted(monkeypatch, replies):
    calls = []

    def fake(cmd, **_k):
        calls.append(list(cmd))
        for prefix, reply in replies:
            if cmd[: len(prefix)] == prefix:
                return reply
        return _R()

    monkeypatch.setattr(isch, "_run", fake)
    return calls


def test_windows_loop_task_runs_in_the_foreground():
    """--background made the task finish at once, so /End could not stop a
    wedged loop; the action is already pythonw.exe (no console)."""
    spec = next(s for s in isch.get_schedule_specs(str(REPO)) if s["name"] == "AgentOS_CognitiveLoop")
    assert "--background" not in spec["args"]
    assert "--log-file" in spec["args"]


def test_launchctl_load_failed_on_stderr_with_exit_zero_is_not_ok(monkeypatch):
    _scripted(
        monkeypatch,
        [
            (["launchctl", "load"], _R(0, err="Load failed: 5: Input/output error")),
            (["launchctl", "list"], _R(113)),
        ],
    )
    ok, detail = isch._launchd_load_verified("/x/com.m3memory.dashboard.plist", "com.m3memory.dashboard")
    assert ok is False
    assert "Load failed" in detail and "not loaded" in detail


def test_launchctl_loaded_with_stderr_is_reported(monkeypatch):
    _scripted(monkeypatch, [(["launchctl", "load"], _R(0, err="Load failed: 5: Input/output error"))])
    ok, detail = isch._launchd_load_verified("/x/a.plist", "com.m3memory.dashboard")
    assert ok is True and "PREVIOUS" in detail and "bootstrap" in detail


def test_launchctl_clean_load_is_ok(monkeypatch):
    _scripted(monkeypatch, [])
    assert isch._launchd_load_verified("/x/a.plist", "l") == (True, "")


@pytest.mark.parametrize("state,ok", [("active", True), ("activating", False), ("failed", False)])
def test_systemd_enable_checks_is_active(monkeypatch, state, ok):
    _scripted(monkeypatch, [(["systemctl", "--user", "is-active"], _R(0 if ok else 3, out=state + "\n"))])
    got, detail = isch._systemd_enable_verified("m3-x.service", settle_s=0)
    assert got is ok
    if not ok:
        assert state in detail and "journalctl --user -u m3-x.service" in detail


def test_systemd_enable_failure_carries_stderr(monkeypatch):
    _scripted(monkeypatch, [(["systemctl", "--user", "enable"], _R(1, err="Unit not found"))])
    ok, detail = isch._systemd_enable_verified("m3-x.service", settle_s=0)
    assert ok is False and "exit=1" in detail and "Unit not found" in detail


@pytest.mark.parametrize(
    "key,reply,expect",
    [
        ("linux", _R(0, out="active\n"), True),
        ("linux", _R(3, out="inactive\n"), False),
        ("darwin", _R(0, out='{\n\t"PID" = 123;\n};'), True),
        ("darwin", _R(0, out="{\n};"), False),
        ("win", _R(0, out='"\\AgentOS_X","N/A","Running"'), True),
        ("win", _R(0, out='"\\AgentOS_X","N/A","Wird ausgefuehrt"'), None),  # localized: cannot tell
    ],
)
def test_supervisor_running(monkeypatch, key, reply, expect):
    monkeypatch.setattr(isch, "_platform_key", lambda: key)
    monkeypatch.setattr(isch, "_run", lambda *a, **k: reply)
    running, state = isch._supervisor_running("svc", deadline_s=0)
    assert running is expect, state
