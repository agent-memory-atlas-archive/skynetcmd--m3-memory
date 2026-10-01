"""An upgrade must not silently drop the cognitive loop.

`m3 setup --non-interactive` leaves `plan.cognitive_loop` False, and
`_step_verify_daemons` only adds a role to `expected` when its plan field is
TRUE:

    if getattr(plan, field):
        expected.append(role)
    if not expected:
        return True          # nothing was enabled -> nothing to verify

So an upgrade that passes neither flag STOPS the loop — step 1, and again after
the package is replaced — and then never restarts or verifies it. A host that
had the loop running comes back WITHOUT it, until the PT30M keep-alive or the
5-minute watchdog happens to fire. That is the opposite of "restart the
cognitive loop on the new version".

The fix reads the CURRENT state before the upgrade and passes the matching flag,
so the operator's choice survives and the loop returns on the new code. These
tests pin the three branches, because the failure is invisible: the upgrade
reports success either way.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN = _ROOT / "bin"
sys.path.insert(0, str(_BIN))

import m3_upgrade as m3u  # noqa: E402


def _src() -> str:
    return (_BIN / "m3_upgrade.py").read_text(encoding="utf-8")


# ── the three branches are wired ─────────────────────────────────────────────

def test_an_installed_loop_is_carried_across_the_upgrade():
    s = _src()
    assert 'setup_cmd.append("--cognitive-loop")' in s, (
        "an upgrade no longer tells setup the loop was installed, so setup will "
        "not restart or verify the loop the upgrade just stopped"
    )


def test_a_declined_loop_stays_declined():
    s = _src()
    assert 'setup_cmd.append("--no-cognitive-loop")' in s, (
        "a host that had deliberately declined the loop must not have one "
        "installed by an upgrade"
    )


def test_an_undeterminable_state_passes_neither_flag_and_says_so():
    """Guessing either way is worse than saying 'untouched'."""
    s = _src()
    i = s.index("could not determine whether the cognitive loop")
    assert i > 0, "the unknown branch no longer explains itself"
    # and it must not fall through into a flag
    tail = s[i:i + 600]
    assert "--cognitive-loop" not in tail, "the unknown branch guesses a flag"


def test_detection_happens_BEFORE_the_package_is_replaced():
    """The detector ships IN the payload being replaced, so the read must come
    first. Order, not mere presence."""
    s = _src()
    detect = s.index("loop_was_installed = cognitive_loop_installed(m3)")
    upgrade = s.index("[2/5] upgrading the package")
    use = s.index("if loop_was_installed is True:")
    assert detect < upgrade < use, (
        "detection must precede the package upgrade, and the flag is used after "
        f"it (detect={detect}, upgrade={upgrade}, use={use})"
    )


# ── the detector itself ──────────────────────────────────────────────────────

def test_detector_returns_None_when_the_package_cannot_be_located(monkeypatch):
    """Undeterminable, not False — False would mean "declined" and could
    uninstall a loop the operator wanted."""
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: None)
    assert m3u.cognitive_loop_installed("m3") is None


def test_detector_returns_None_when_the_detector_module_is_absent(tmp_path, monkeypatch):
    """A payload without governor_migration.py yields unknown, never a guess."""
    pkg = tmp_path / "m3_memory"
    (pkg / "bin").mkdir(parents=True)
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)
    assert m3u.cognitive_loop_installed("m3") is None


@pytest.mark.parametrize("printed,expected", [("YES", True), ("NO", False)])
def test_detector_reads_the_subprocess_verdict(tmp_path, monkeypatch, printed, expected):
    pkg = tmp_path / "m3_memory"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "bin" / "governor_migration.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)

    class _R:
        returncode = 0
        stdout = printed + "\n"
        stderr = ""

    monkeypatch.setattr(m3u.subprocess, "run", lambda *a, **k: _R())
    assert m3u.cognitive_loop_installed("m3") is expected


def test_detector_returns_None_on_unexpected_output(tmp_path, monkeypatch):
    """A detector that printed a traceback must not be read as 'declined'."""
    pkg = tmp_path / "m3_memory"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "bin" / "governor_migration.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(m3u, "find_m3_package", lambda exe: pkg)

    class _R:
        returncode = 1
        stdout = "Traceback (most recent call last):\n"
        stderr = "boom"

    monkeypatch.setattr(m3u.subprocess, "run", lambda *a, **k: _R())
    assert m3u.cognitive_loop_installed("m3") is None


def test_detector_never_imports_the_payload_into_this_process():
    """This script must keep working while the package it would import is being
    deleted — the reason it shells out for everything."""
    s = _src()
    assert "import governor_migration" not in s.split("def cognitive_loop_installed")[0], (
        "m3_upgrade.py imports the payload at module scope; the upgrade deletes "
        "that package mid-run"
    )
