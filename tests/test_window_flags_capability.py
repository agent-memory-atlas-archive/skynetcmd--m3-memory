"""Window-hiding kwargs follow the CAPABILITY, not the OS identity.

subprocess.CREATE_NO_WINDOW and subprocess.STARTUPINFO exist only on a Windows
interpreter. When the helpers keyed on "is this Windows?", a test simulating
Windows on Linux/macOS reached them and raised AttributeError (six failures on
the POSIX lanes, 2026-10-10). Emulated here on any OS by removing the
attributes, as a POSIX interpreter lacks them.
"""

from __future__ import annotations

import subprocess

from conftest import simulate_os


def _as_posix_interpreter(monkeypatch):
    for attr in ("CREATE_NO_WINDOW", "STARTUPINFO"):
        monkeypatch.delattr(subprocess, attr, raising=False)


def test_simulated_windows_on_a_posix_interpreter_gets_no_flags(monkeypatch):
    from _task_runtime import no_window_kwargs
    from m3_core.seams import hidden_window_kwargs

    _as_posix_interpreter(monkeypatch)
    simulate_os(monkeypatch, "Windows")
    assert no_window_kwargs() == {}
    assert hidden_window_kwargs() == {}


def test_a_windows_interpreter_gets_the_flags(monkeypatch):
    from _task_runtime import no_window_kwargs
    from m3_core.seams import hidden_window_kwargs

    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    class _SI:
        dwFlags = 0
        wShowWindow = None

    monkeypatch.setattr(subprocess, "STARTUPINFO", _SI, raising=False)
    monkeypatch.setattr(subprocess, "STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr(subprocess, "SW_HIDE", 0, raising=False)
    assert no_window_kwargs() == {"creationflags": 0x08000000}
    assert "startupinfo" in hidden_window_kwargs()
