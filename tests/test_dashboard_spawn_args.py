"""The detached dashboard child gets its address as --host/--port, not through env: an env handoff is ignored once the child reads config, and it would
fall back to the default address without a word.

The launcher must also report what the child actually did (listening / exited /
not yet answering) instead of announcing "started" unchecked, and the child must
get --log-file because its stdio is DEVNULL."""

from __future__ import annotations

import importlib
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))


class _Popen:
    rc: int | None = None
    seen: list[tuple[list[str], dict]] = []

    def __init__(self, argv, **kwargs):
        type(self).seen.append((list(argv), kwargs))
        self.pid = 4242

    def poll(self):
        return type(self).rc


def _spawn(monkeypatch, tmp_path, *, rc, listening):
    import dashboard_server

    _Popen.seen = []
    _Popen.rc = rc
    monkeypatch.setattr(subprocess, "Popen", _Popen)
    monkeypatch.setattr(dashboard_server, "_port_holder_is_dashboard", lambda *a, **k: listening)
    monkeypatch.setattr(dashboard_server, "_SPAWN_CONFIRM_S", 0.0)
    monkeypatch.setattr(dashboard_server.time, "sleep", lambda _s: None)
    paths = importlib.import_module("m3_core.paths")
    monkeypatch.setattr(paths, "get_m3_logs_root", lambda: str(tmp_path))
    return dashboard_server._spawn_detached("192.0.2.5", 9099)


def test_the_respawn_passes_host_and_port_as_arguments(monkeypatch, capsys, tmp_path):
    assert _spawn(monkeypatch, tmp_path, rc=None, listening=True) == 0
    ((argv, kwargs),) = _Popen.seen
    i = argv.index("--foreground")
    assert argv[i : i + 5] == ["--foreground", "--host", "192.0.2.5", "--port", "9099"]
    assert "env" not in kwargs, "the child's address must not travel through env"


def test_the_child_gets_a_log_file(monkeypatch, capsys, tmp_path):
    _spawn(monkeypatch, tmp_path, rc=None, listening=True)
    ((argv, _kw),) = _Popen.seen
    assert argv[argv.index("--log-file") + 1] == str(tmp_path / "dashboard.log")


def test_started_is_reported_only_once_the_child_answers(monkeypatch, capsys, tmp_path):
    assert _spawn(monkeypatch, tmp_path, rc=None, listening=True) == 0
    out = capsys.readouterr().out
    assert "started in background (pid 4242)" in out


def test_a_child_that_exits_is_a_failure_naming_the_exit_code_and_log(monkeypatch, capsys, tmp_path):
    assert _spawn(monkeypatch, tmp_path, rc=3, listening=None) == 1
    cap = capsys.readouterr()
    assert "started in background" not in cap.out
    assert "exited with code 3" in cap.err
    assert "dashboard.log" in cap.err


def test_a_slow_child_is_not_reported_as_started(monkeypatch, capsys, tmp_path):
    _spawn(monkeypatch, tmp_path, rc=None, listening=None)
    cap = capsys.readouterr()
    assert "started in background" not in cap.out
    assert "NOT yet answering" in cap.err


def test_stop_reports_failure_when_the_process_survives(monkeypatch, capsys):
    import dashboard_server as D

    monkeypatch.setattr(D, "_live_dashboards", lambda: [])
    monkeypatch.setattr(D, "_dashboard_pids_by_cmdline", lambda: {4242})
    monkeypatch.setattr(D, "_kill_pid", lambda pid: "taskkill exited 1: Access is denied.")
    assert D.dashboard_stop() == 1
    cap = capsys.readouterr()
    assert "stopped (pid" not in cap.out
    assert "could NOT stop pid 4242" in cap.err and "Access is denied" in cap.err


def test_a_foreign_listener_is_not_reported_as_the_dashboard(monkeypatch, capsys):
    import dashboard_server as D

    monkeypatch.setattr(D, "_assess_dashboard_auth", lambda host: D._AuthAssessment(None, False))
    monkeypatch.setattr(D, "_live_dashboards", lambda: [])
    monkeypatch.setattr(D, "_dashboard_pids_by_cmdline", lambda: set())
    monkeypatch.setattr(D, "_port_holder_is_dashboard", lambda *a, **k: False)
    monkeypatch.setattr(D, "_spawn_detached", lambda *a: (_ for _ in ()).throw(AssertionError("must not spawn")))
    assert D.run_dashboard("127.0.0.1", 18098, background=True) == 1
    cap = capsys.readouterr()
    assert "already serving" not in cap.out
    assert "ANOTHER process" in cap.err


def test_chatlog_decay_is_refused_when_the_chatlog_store_does_not_resolve(monkeypatch):
    import asyncio

    import chatlog_config
    import dashboard_server as D

    def _boom():
        raise RuntimeError("config unreadable (test)")

    monkeypatch.setattr(chatlog_config, "resolve_config", _boom)
    monkeypatch.setitem(D.active_task, "process", None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    r = asyncio.run(D.trigger_maintenance_task("decay_apply"))
    assert r.status_code == 500
    assert b"config unreadable" in r.body and b"main store" in r.body
