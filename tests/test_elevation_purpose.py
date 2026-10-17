"""Every UAC prompt is framed the same way, by the primitive.

UAC shows no text a program supplies: the dialog names the program (Windows
PowerShell) and, under "Show more details", its command line. So
ElevationBatch.run prints the standard notice and the steps before the dialog,
carries the steps on the elevated command line, and afterwards says
"permission approved | disapproved | timed out" on the console and in
<logs root>/elevation.log.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from types import SimpleNamespace

import pytest

from m3_memory import elevate
from m3_memory._roots import logs_root


def _batch():
    b = elevate.ElevationBatch()
    b.add("stop the embed service", ["m3-embed-server", "stop"])
    b.add('re-register the "embed" service', ["m3-embed-server", "install"])
    return b


def test_purpose_names_every_step_and_holds_no_double_quote():
    assert _batch().purpose() == "m3: stop the embed service; re-register the 'embed' service"


@pytest.fixture
def windows_launcher(monkeypatch, tmp_path):
    """A Windows host whose launcher is faked: ``behave(argv, results_path)``
    decides what the 'elevated process' did. Yields (set_behaviour, seen)."""
    monkeypatch.setattr(elevate, "is_windows", lambda: True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(elevate, "_workdir", lambda: str(work))
    monkeypatch.setattr(elevate, "_remove_workdir", lambda _p: None)
    seen: dict = {}
    behaviour: dict = {}

    def run(argv, **_k):
        seen["launcher"] = argv[-1]
        return behaviour["fn"](str(work / "results.json"))

    monkeypatch.setattr(elevate.subprocess, "run", run)
    return (lambda fn: behaviour.__setitem__("fn", fn)), seen


def _log_tail() -> str:
    with open(os.path.join(logs_root(), "elevation.log"), encoding="utf-8") as fh:
        return fh.read().splitlines()[-1]


def test_approved(windows_launcher, capsys):
    behave, seen = windows_launcher

    def approved(results):
        with open(results, "w", encoding="utf-8") as fh:
            json.dump({"stop the embed service": 0, 're-register the "embed" service': 0}, fh)
        return SimpleNamespace(returncode=0)

    behave(approved)
    b = _batch()
    out = b.run(timeout=5)
    printed = capsys.readouterr().out
    assert printed.index(elevate.ELEVATION_NOTICE) < printed.index("permission approved")
    assert "  - stop the embed service" in printed
    assert b.outcome == "approved" and out["stop the embed service"]["rc"] == 0
    # Inside the PowerShell single-quoted Arguments string, the ' of 'embed' is doubled.
    assert "\"m3: stop the embed service; re-register the ''embed'' service\"" in seen["launcher"]
    line = _log_tail()
    # Leads with the UTC end time, so the file sorts.
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ permission approved \| requested ", line), line
    assert line.endswith("| m3: stop the embed service; re-register the 'embed' service")
    # Both times, each local with its zone and then UTC.
    assert line.count("Z) |") == 2 and " | ended " in line and " | after " in line
    assert "  requested " in printed and "Z)" in printed.split("requested ")[1].splitlines()[0]


def test_disapproved(windows_launcher, capsys):
    behave, _seen = windows_launcher
    behave(lambda _r: SimpleNamespace(returncode=1223))  # ERROR_CANCELLED, no results
    b = _batch()
    assert b.run(timeout=5) is None
    assert "permission disapproved" in capsys.readouterr().out
    assert b.outcome == "disapproved" and "Z permission disapproved | requested " in _log_tail()


def test_no_answer_before_the_deadline_is_timed_out(windows_launcher, capsys):
    behave, _seen = windows_launcher

    def hang(_r):
        raise subprocess.TimeoutExpired("powershell", 5)

    behave(hang)
    b = _batch()
    assert b.run(timeout=5) is None
    out = capsys.readouterr().out
    assert b.outcome == "timed out"
    assert "permission timed out (no answer within 5s; a later approval runs nothing)" in out


def test_approved_after_the_deadline_is_timed_out(windows_launcher, capsys):
    behave, _seen = windows_launcher

    def late(results):
        with open(results, "w", encoding="utf-8") as fh:
            json.dump({"expired": 1}, fh)
        return SimpleNamespace(returncode=0)

    behave(late)
    b = _batch()
    assert b.run(timeout=5) is None and b.outcome == "timed out"
    # The same word, a different condition, said so.
    assert "permission timed out (approved after the 5s deadline; nothing ran)" in capsys.readouterr().out


def test_a_launcher_failure_is_not_called_a_refusal(windows_launcher, capsys):
    behave, _seen = windows_launcher
    behave(lambda _r: SimpleNamespace(returncode=9009))
    b = _batch()
    assert b.run(timeout=5) is None
    out = capsys.readouterr().out
    assert "disapproved" not in out and "permission not confirmed (no results; launcher exit 9009)" in out


def test_an_elevated_process_is_told_no_prompt_appears(windows_launcher, monkeypatch, capsys):
    behave, _seen = windows_launcher
    monkeypatch.setattr(elevate, "is_elevated", lambda: True)

    def ran(results):
        with open(results, "w", encoding="utf-8") as fh:
            json.dump({"stop the embed service": 0, 're-register the "embed" service': 0}, fh)
        return SimpleNamespace(returncode=0)

    behave(ran)
    b = _batch()
    b.run(timeout=5)
    out = capsys.readouterr().out
    assert "already elevated: no prompt appears" in out
    assert b.outcome == elevate.NOT_NEEDED and "permission approved" not in out
