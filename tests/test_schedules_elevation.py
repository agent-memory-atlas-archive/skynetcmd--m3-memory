"""`m3 schedules add/remove/repair` on Windows: never ask to elevate from an
already elevated shell, and never ask when nobody can answer (a hidden elevated
run waited for ever on "Run it elevated now?", 2026-10-11)."""

from __future__ import annotations

import argparse


def test_is_elevated_reads_the_uid_where_there_is_one(monkeypatch):
    from m3_memory import _platform

    monkeypatch.setattr(_platform, "current_uid", lambda: 0)
    assert _platform.is_elevated() is True
    monkeypatch.setattr(_platform, "current_uid", lambda: 1000)
    assert _platform.is_elevated() is False


def _args(sub):
    return argparse.Namespace(schedules_cmd=sub, name="AgentOS_NotificationWaiter", port=None)


def test_an_elevated_shell_is_not_asked_to_elevate(monkeypatch):
    from m3_memory import cli

    ran = []
    monkeypatch.setattr(cli, "is_windows", lambda: True)
    monkeypatch.setattr(cli, "is_elevated", lambda: True)
    monkeypatch.setattr(
        cli, "_elevate_schedules_windows", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked"))
    )
    monkeypatch.setattr(cli, "_run_bin_script", lambda script, argv: ran.append(argv) or 0)
    assert cli._cmd_schedules(_args("add")) == 0
    assert ran == [["--add", "AgentOS_NotificationWaiter"]]


def test_no_person_at_the_console_is_refused_not_prompted(monkeypatch, capsys):
    from m3_memory import cli
    from m3_memory.wizard import ui

    monkeypatch.setattr(cli, "is_windows", lambda: True)
    monkeypatch.setattr(cli, "is_elevated", lambda: False)
    monkeypatch.setattr(ui, "console_has_person", lambda: False)
    monkeypatch.setattr(
        cli, "_elevate_schedules_windows", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked"))
    )
    monkeypatch.setattr(cli, "_run_bin_script", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran unelevated")))
    assert cli._cmd_schedules(_args("repair")) == 1
    assert "elevated PowerShell" in capsys.readouterr().out
