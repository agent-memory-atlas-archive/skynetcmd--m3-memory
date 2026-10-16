"""`m3 doctor`'s dispatch doorbell report: never fails the doctor, silent in brief
mode when the core has no daemon, and says why when one should be answering."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from doctor import dispatch_probe  # noqa: E402
from m3_core import doorbell  # noqa: E402
from m3_core.seams import core_bins  # noqa: E402


def _bins(monkeypatch, daemon, waiter=None):
    monkeypatch.setattr(core_bins, "core_binary", lambda base: {"m3-dispatchd": daemon, "m3-wait": waiter}.get(base))


def test_no_daemon_in_the_core_is_quiet_in_brief(monkeypatch, capsys):
    _bins(monkeypatch, None)
    assert dispatch_probe.run(brief=True) == 0
    assert capsys.readouterr().out == ""
    assert dispatch_probe.run(brief=False) == 0
    assert "polls (supported)" in capsys.readouterr().out


def test_a_daemon_that_should_answer_but_does_not_is_reported(monkeypatch, capsys):
    _bins(monkeypatch, pathlib.Path("/core/m3-dispatchd"))
    monkeypatch.setattr(doorbell, "status", lambda ep: None)
    assert dispatch_probe.run(brief=True) == 0, "report-only: polling still delivers"
    out = capsys.readouterr().out
    assert "not answering" in out and "m3-dispatchd.log" in out and "waiter" in out


def test_a_serving_daemon_shows_its_readers(monkeypatch, capsys):
    _bins(monkeypatch, pathlib.Path("/core/m3-dispatchd"), pathlib.Path("/core/m3-wait"))
    view = {
        "rings_logged": 3,
        "readers": {"a": {"pending": 1, "oldest_pending_ms": 1500, "waiting": 1, "wakes": 4}},
    }
    monkeypatch.setattr(doorbell, "status", lambda ep: view)
    assert dispatch_probe.run(brief=True) == 0
    assert "1 reader set(s), 1 waiting" in capsys.readouterr().out
    assert dispatch_probe.run(brief=False) == 0
    out = capsys.readouterr().out
    assert "pending 1, oldest 1.5s, waiting 1, wakes 4" in out
