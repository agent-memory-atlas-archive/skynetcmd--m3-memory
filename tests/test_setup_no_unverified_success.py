"""`m3 setup` must not claim a service restarted when it only issued a start.

Measured 2026-09-30 on claude-dev (Linux, systemd --user), running
`m3 setup --non-interactive --cognitive-loop --force-quiesce`:

    [OK]   cognitive-loop: restarted
    [!]    cognitive-loop: NOT running

Back to back, in one run. No cognitive-loop unit file existed at all, so nothing
was installed and nothing could start. The `[OK]` came from
`_start_service_for_role`, whose contract is "True if the start was ISSUED" —
install_schedules guards on `_service_exists`, silently does nothing when no unit
is present, and returns success anyway.

The verdict that followed was correct, so this was not a wrong outcome; it was a
success CLAIM for work that had not been checked, which is the same
silent-success class `_step_doctor` was fixed for (it used to `return True` in
both branches). A reader who sees `[OK] restarted` stops reading.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from m3_memory import setup_wizard as sw  # noqa: E402

# Asserted on the SOURCE rather than by driving `_step_verify_daemons`:
# that function reads a process registry through several module-level
# indirections, and a fake registry pins the shape of the fake more than
# the behaviour under test. The claim here is narrow and textual — "no
# success line may assert a restart that was never verified" — so test it
# where it lives. The runtime behaviour is covered by the doctor probes.


def test_no_OK_claims_a_restart_before_verification():
    """THE regression, asserted on the SOURCE so it holds regardless of how the
    registry is faked: no success line may say 'restarted'."""
    src = (_ROOT / "m3_memory" / "setup_wizard.py").read_text(encoding="utf-8")
    assert '_ok(f"  {role}: restarted")' not in src, (
        "setup again claims `[OK] <role>: restarted` from _start_service_for_role, "
        "whose contract is only that the start was ISSUED. Report the attempt and "
        "let the post-restart registry re-read deliver the verdict."
    )


def test_the_attempt_is_still_reported():
    """Silence would be worse than the false claim — the user must see that a
    restart was tried, otherwise an unexplained pause looks like a hang."""
    src = (_ROOT / "m3_memory" / "setup_wizard.py").read_text(encoding="utf-8")
    assert "start issued" in src, "the restart attempt is no longer reported at all"


def test_the_verified_running_line_survives():
    """The positive signal must still exist, printed from the RE-READ registry."""
    src = (_ROOT / "m3_memory" / "setup_wizard.py").read_text(encoding="utf-8")
    assert '_ok(f"  {role}: running")' in src, (
        "the verified success line is gone; without it a healthy service reports "
        "nothing and the step looks like it did nothing"
    )


def test_start_service_for_role_documents_that_it_only_issues():
    """The contract this fix depends on. If it ever starts VERIFYING, the caller
    may legitimately claim success again — and this test should be revisited."""
    doc = sw._start_service_for_role.__doc__ or ""
    assert "issued" in doc.lower(), (
        "_start_service_for_role no longer documents that it merely issues the "
        "start; re-check whether the caller's wording should change too"
    )
