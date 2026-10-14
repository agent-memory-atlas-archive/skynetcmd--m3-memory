"""The detached dashboard child gets its address as --host/--port, not through env: an env handoff is ignored once the child reads config, and it would
fall back to the default address without a word."""

from __future__ import annotations

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))


def test_the_respawn_passes_host_and_port_as_arguments(monkeypatch, capsys):
    import dashboard_server

    seen: list[tuple[list[str], dict]] = []

    class _Popen:
        def __init__(self, argv, **kwargs):
            seen.append((list(argv), kwargs))

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    dashboard_server._spawn_detached("192.0.2.5", 9099)
    ((argv, kwargs),) = seen
    assert argv[-5:] == ["--foreground", "--host", "192.0.2.5", "--port", "9099"]
    assert "env" not in kwargs, "the child's address must not travel through env"
