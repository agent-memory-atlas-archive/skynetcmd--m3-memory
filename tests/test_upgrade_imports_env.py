"""`m3 upgrade` runs `m3 config import-env --non-interactive` after setup and
before doctor (plan 2F.1 slice 4): a disagreement is listed, never decided, and
a failure never fails the upgrade (m3 7200dbed)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import m3_upgrade as m3u  # noqa: E402


def _src() -> str:
    return (Path(__file__).resolve().parents[1] / "bin" / "m3_upgrade.py").read_text(encoding="utf-8")


def test_import_env_runs_after_setup_succeeds_and_before_doctor():
    src = _src()
    setup_fail = src.index("`m3 setup` failed (exit")
    step = src.index('import_cmd = [m3, "config", "import-env", "--non-interactive"]')
    doctor = src.index('doctor_cmd = [m3, "doctor", "--fix", "--fix-hooks"]')
    assert setup_fail < step < doctor


def test_a_failed_import_does_not_end_the_upgrade():
    block = _src().split("rc_import = run(import_cmd", 1)[1].split("doctor_cmd =", 1)[0]
    assert "return" not in block, "a failed import-env must not stop the upgrade"
    assert "settings still work where they are" in block


@pytest.mark.parametrize(
    "rc, out, expected",
    [
        (0, "usage: m3 config {show,get,set,unset,import-env}", True),
        (0, "usage: m3 config {show}", False),
        (2, "", False),
    ],
)
def test_the_step_runs_only_when_this_m3_has_the_command(monkeypatch, rc, out, expected):
    monkeypatch.setattr(m3u, "run_captured_rc", lambda cmd, timeout=60: (rc, out))
    assert m3u._has_import_env("m3") is expected


def test_a_dry_run_shows_the_step_without_probing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a dry run runs nothing")

    monkeypatch.setattr(m3u, "run_captured_rc", boom)
    assert m3u._has_import_env("m3", dry=True) is True
