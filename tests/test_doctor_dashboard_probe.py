"""dashboard_probe: a failed kill or relaunch is reported with its exit code and
output, never as success (bin/doctor/dashboard_probe.py). No real process runs."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from doctor import dashboard_probe as dp  # noqa: E402


class _R:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_a_failed_taskkill_is_not_reported_as_killed(monkeypatch, capsys):
    monkeypatch.setattr(dp, "is_windows", lambda: True)
    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **k: _R(128, err="ERROR: Access is denied."))
    assert dp._kill(4242) is False
    out = capsys.readouterr().out
    assert "exited 128" in out and "Access is denied" in out and "taskkill /F /PID 4242" in out


def test_a_failed_relaunch_names_its_exit_code_and_the_foreground_command(monkeypatch, capsys):
    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **k: _R(2, err="ModuleNotFoundError: fastapi"))
    monkeypatch.setattr(dp, "_port_serving", lambda *a, **k: False)
    assert dp._restart("127.0.0.1", 8088) is False
    out = capsys.readouterr().out
    assert "exited 2" in out and "fastapi" in out
    assert "m3 dashboard --foreground --host 127.0.0.1 --port 8088" in out
