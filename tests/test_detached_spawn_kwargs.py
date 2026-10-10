"""`_platform.detached_spawn_kwargs()`: a background child with no console window.

The dashboard daemon and doctor's embed-server fix each carried their own copy
of this pair; the seam owns it now. Capability-driven, so both branches run on
every OS.
"""

from __future__ import annotations

import subprocess

import pytest

from conftest import simulate_os


def test_without_the_windows_flag_it_detaches_the_session(monkeypatch):
    from m3_memory._platform import detached_spawn_kwargs

    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    assert detached_spawn_kwargs() == {"start_new_session": True}


@pytest.mark.requires_windows  # the real flags exist only there
def test_on_windows_it_hides_and_detaches():
    from m3_memory._platform import detached_spawn_kwargs

    flags = detached_spawn_kwargs()["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW and flags & subprocess.DETACHED_PROCESS, (
        f"observed: creationflags={flags:#x}. inspect: m3_memory/_platform.py detached_spawn_kwargs"
    )


def test_a_simulated_windows_on_posix_does_not_reach_missing_flags(monkeypatch):
    from m3_memory._platform import detached_spawn_kwargs

    simulate_os(monkeypatch, "Windows")
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    assert detached_spawn_kwargs() == {"start_new_session": True}
