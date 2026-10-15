"""Setup reports the dashboard's boot task as working only when it is: the
scheduler exits 0 when the unit was written but could not be enabled (no
systemd user session), and setup printed "[OK] dashboard starts on boot"."""

from __future__ import annotations

import subprocess
import types


def _run_with(monkeypatch, rc: int, out: str, capsys) -> str:
    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.os.path, "exists", lambda p: True)
    monkeypatch.setattr(sw, "_called_by_upgrade", lambda: False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=rc, stdout=out, stderr=""))
    sw._register_dashboard_task(8088)
    return capsys.readouterr().out


def test_a_unit_that_could_not_be_enabled_is_not_reported_as_starting(monkeypatch, capsys):
    out = _run_with(monkeypatch, 0, "[FAIL] systemctl enable --now failed: no user bus\n", capsys)
    assert "[OK] dashboard starts on boot" not in out and "[FAIL]" in out


def test_a_working_unit_is_reported(monkeypatch, capsys):
    out = _run_with(monkeypatch, 0, "[OK] installed\n", capsys)
    assert "[OK] dashboard starts on boot" in out
