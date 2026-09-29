"""The simulated-OS guard: `os.name` / `sys.platform` must not outlive a test.

43 call sites across 8 test files simulate another OS with
``monkeypatch.setattr(<mod>.sys, "platform", "linux")`` or
``monkeypatch.setattr(<mod>.os, "name", "nt")``. Because ``<mod>.os`` and
``<mod>.sys`` ARE the global modules, every one of those overrides is
process-wide, and monkeypatch only undoes them *after* the test's report is
built — too late for pytest's own reporter and too late for any other fixture's
teardown. conftest closes that window in two places:

  * ``pytest_runtest_makereport`` (hookwrapper, tryfirst) — fires for the
    ``call`` phase BEFORE any teardown fixture runs. This is the only place that
    can stop a simulated platform from reaching *another fixture's* teardown.
  * ``_restore_platform_identity`` (autouse fixture) — keeps a leak from
    reaching the *next test*.

Both restore from the session constants ``_REAL_OS_NAME`` /
``_REAL_SYS_PLATFORM``, captured at conftest import.

Why this file exists: on 2026-09-28 only the ``os.name`` half was guarded.
``test_fips_integrity.py``'s crypto-repair finalizer reloads
``crypto_provider``, and the two tests that set ``sys.platform = "linux"`` left
it live through that reload — so on a macOS host the resolver looked for
``libwolfssl.so`` rather than the real ``libwolfssl.dylib``, found nothing, and
under ``M3_FIPS_MODE=1`` correctly failed closed. The result was two teardown
ERRORs whose message described a machine state that did not exist, on a box that
was perfectly healthy. Nothing pinned the contract, so nothing stopped the
asymmetry from persisting.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest


def _conftest_constants() -> tuple[str, str]:
    """The session constants conftest captured at import."""
    import conftest
    return conftest._REAL_OS_NAME, conftest._REAL_SYS_PLATFORM


# A simulated value must DIFFER from the real one or the test has no teeth. The
# first draft hardcoded "linux", which is the real platform on the Linux box: the
# control (guard removed) passed there because nothing had changed, so the test
# silently proved nothing on one of the three supported OSes. Derive the
# simulation from the host instead.
_FAKE_PLATFORM = "m3-simulated-platform"   # never a real sys.platform value


def _fake_os_name() -> str:
    """An `os.name` that is not this host's. Kept to the two values pathlib
    understands: an unknown string would silently select Posix flavour, which on
    Windows is a different test than the one intended."""
    return "posix" if os.name == "nt" else "nt"


# ── the session constants must be the TRUE identity ──────────────────────────

def test_session_constants_match_an_untouched_interpreter():
    """Captured at conftest import — verify that moment was actually clean.

    Everything else here restores FROM these constants, so if conftest were ever
    imported after something had already patched the identity, both guards would
    faithfully restore a lie and no test would notice. Checked against a fresh
    subprocess, which no in-process monkeypatching can reach.
    """
    real_name, real_platform = _conftest_constants()
    out = subprocess.run(
        [sys.executable, "-c", "import os,sys;print(os.name);print(sys.platform)"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout.split()
    assert [real_name, real_platform] == out, (
        f"conftest captured {real_name!r}/{real_platform!r} but an untouched "
        f"interpreter reports {out} — the capture happened after a patch"
    )


def test_live_identity_matches_the_session_constants():
    """At the start of any test, the identity is the real one — i.e. no previous
    test's simulation survived into this one."""
    real_name, real_platform = _conftest_constants()
    assert (os.name, sys.platform) == (real_name, real_platform)


# ── a simulation must not reach another fixture's teardown ───────────────────

@pytest.fixture()
def identity_at_teardown(monkeypatch):
    """Assert the real identity is back by the time THIS fixture tears down.

    Depends on `monkeypatch` deliberately: a fixture that requests another is
    set up after it and therefore torn down BEFORE it. That ordering is the whole
    point — it puts this assertion inside the window where monkeypatch has not
    yet undone anything, which is exactly where the crypto-repair finalizer sits.
    Without that dependency this fixture would tear down after monkeypatch had
    already restored, and would pass even with the guard removed.
    """
    expected = _conftest_constants()
    yield monkeypatch
    assert (os.name, sys.platform) == expected, (
        "a simulated platform identity survived into another fixture's "
        f"teardown: {(os.name, sys.platform)} != {expected}. Any teardown that "
        "resolves a per-OS filename will now pick the wrong one."
    )


def test_simulated_platform_does_not_reach_a_later_teardown(identity_at_teardown):
    """THE 2026-09-28 DEFECT, reduced to its shape."""
    monkeypatch = identity_at_teardown
    monkeypatch.setattr(sys, "platform", _FAKE_PLATFORM)
    assert sys.platform == _FAKE_PLATFORM, "the simulation itself must still work"


def test_simulated_os_name_does_not_reach_a_later_teardown(identity_at_teardown):
    """The half that was already guarded — pinned so it stays that way."""
    monkeypatch = identity_at_teardown
    fake = _fake_os_name()
    monkeypatch.setattr(os, "name", fake)
    assert os.name == fake


def test_both_simulated_at_once(identity_at_teardown):
    """How the FIPS tests actually do it: both axes in one test."""
    monkeypatch = identity_at_teardown
    fake = _fake_os_name()
    monkeypatch.setattr(os, "name", fake)
    monkeypatch.setattr(sys, "platform", _FAKE_PLATFORM)
    assert (os.name, sys.platform) == (fake, _FAKE_PLATFORM)


# ── the observable consequence, asserted directly ────────────────────────────

def test_path_flavour_is_native_once_the_simulation_is_undone(identity_at_teardown):
    """`pathlib.Path()` picks PosixPath vs WindowsPath from `os.name` at
    construction time, and pytest builds one in `repr_failure`. A leak turns the
    next unrelated failure into a session-aborting INTERNALERROR.

    ⚠ Do NOT construct a Path while the simulation is live — this test
    deliberately does not. Constructing an off-host flavour raises
    `NotImplementedError: cannot instantiate %r on your system` on Python 3.11
    (measured on Windows with `os.name` forced to "posix"). 3.11 is below this
    project's floor of >=3.12, where both flavours construct happily, so that
    particular crash is not a supported-matrix concern — but the habit is still
    wrong: it constructs a path the host cannot represent for no reason, and it
    is how a draft of this very test reproduced the class of crash it exists to
    prevent.
    """
    native = type(pathlib.Path("."))
    monkeypatch = identity_at_teardown
    monkeypatch.setattr(os, "name", _fake_os_name())
    monkeypatch.undo()
    assert type(pathlib.Path(".")) is native
