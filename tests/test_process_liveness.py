"""Pins `m3_core.process`, the single owner of PID liveness and identity.

Three copies of "is this PID alive?" disagreed: one read another user's process
as dead, one matched PIDs by substring (12 matched a running 1234). These tests
pin the owner's contract on every OS and fail if a copy reappears.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from m3_core import process  # noqa: E402


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_own_pid_is_alive():
    assert process.pid_is_alive(os.getpid()) is True


@pytest.mark.parametrize("pid", [0, -1])
def test_nonpositive_pid_is_not_alive(pid):
    assert process.pid_is_alive(pid) is False


def test_an_exited_process_is_not_alive():
    assert process.pid_is_alive(_dead_pid()) is False


def _fake_posix(monkeypatch, exc):
    def kill(pid, sig):
        raise exc
    monkeypatch.setattr(process, "os", types.SimpleNamespace(name="posix", kill=kill))


@pytest.mark.parametrize("exc, expected", [
    (ProcessLookupError(), False),   # no such process
    (PermissionError(), True),       # another user's process EXISTS
    (OSError(22, "EINVAL"), True),   # cannot disprove existence
    (OverflowError(), True),
])
def test_posix_branch_contract_on_every_os(monkeypatch, exc, expected):
    """Runs the POSIX branch even on Windows. PermissionError -> dead was the
    version_drift defect: a live server owned by another user read as gone."""
    _fake_posix(monkeypatch, exc)
    assert process.pid_is_alive(4242) is expected


def test_live_owner_matches_own_create_time():
    ct = process.proc_create_time(os.getpid())
    if ct is None:
        pytest.skip("psutil unavailable: create_time unreadable")
    assert process.pid_is_live_owner(os.getpid(), ct) is True


def test_live_owner_rejects_a_reused_pid():
    """Same live PID, different recorded create_time = a different process."""
    ct = process.proc_create_time(os.getpid())
    if ct is None:
        pytest.skip("psutil unavailable: create_time unreadable")
    assert process.pid_is_live_owner(os.getpid(), ct - 10.0) is False


def test_live_owner_without_a_record_falls_back_to_liveness():
    assert process.pid_is_live_owner(os.getpid(), None) is True
    assert process.pid_is_live_owner(_dead_pid(), None) is False


def test_live_owner_uses_injected_probes():
    """m3_halt passes its own (patchable) probes; they must be the ones used."""
    assert process.pid_is_live_owner(
        1, 100.0, alive=lambda p: True, create_time=lambda p: 100.05) is True
    assert process.pid_is_live_owner(
        1, 100.0, alive=lambda p: True, create_time=lambda p: 105.0) is False
    assert process.pid_is_live_owner(
        1, 100.0, alive=lambda p: False, create_time=lambda p: 100.0) is False


def test_former_copies_delegate_to_the_owner():
    import m3_halt
    import version_drift
    from m3_core import locking

    for probe in (version_drift._pid_is_alive, locking._pid_alive, m3_halt._pid_is_alive,
                  m3_halt.pid_is_alive):
        assert probe(os.getpid()) is True
        assert probe(0) is False


_PROBE = re.compile(r"os\.kill\([^)]*,\s*0\)|OpenProcess\(|PID eq")


def test_no_second_pid_probe_exists():
    """§10a: a copied predicate is the defect. Any new PID probe outside the
    owner fails here -- import `m3_core.process` instead."""
    offenders = []
    for root in (_BIN, _BIN.parent / "m3_memory"):
        for f in root.rglob("*.py"):
            if f.name == "process.py" and f.parent.name == "m3_core":
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if _PROBE.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{f.relative_to(_BIN.parent)}:{n}: {line.strip()}")
    assert not offenders, "PID probe outside m3_core.process:\n" + "\n".join(offenders)


# -- launch-chain classification: a pure function, so a table, no patching ----

@pytest.mark.parametrize("name, cmdline, own", [
    ("m3.exe", ["m3"], True),                                   # pipx launcher (Windows)
    ("m3", ["/home/u/.local/bin/m3", "serve"], True),           # launcher (POSIX)
    ("python.exe", ["python.exe", "C:/u/.local/bin/m3.exe"], True),
    ("python3", ["python3", "-m", "m3_memory.cli", "mcp"], True),
    ("python3", ["python3", "bin/memory_bridge.py"], True),
    ("cmd.exe", ["cmd", "/c", "m3"], True),                     # client wraps in a shell
    ("bash", ["bash", "-c", "m3 agent agent_list"], True),
    ("claude.exe", ["claude.exe", "--x"], False),               # the agent's client
    ("python3", ["python3", "-m", "aider"], False),             # a Python-based client
    ("bash", ["bash", "-l"], False),                            # an interactive shell
    ("node", ["node", "gemini.js"], False),
])
def test_launch_chain_classification(name, cmdline, own):
    assert process.is_m3_launch_step(name, cmdline) is own


_VENV_ARGV = ["C:/v/Scripts/python.exe", "-c", "print(1)"]


@pytest.mark.parametrize("name, cmdline, below, redirector", [
    ("python.exe", _VENV_ARGV, _VENV_ARGV, True),            # the Windows venv launcher
    ("python.exe", _VENV_ARGV, _VENV_ARGV + ["x"], False),   # different argv: a real parent
    ("python.exe", _VENV_ARGV, None, False),                 # nothing below to compare
    ("claude.exe", ["claude.exe"], ["claude.exe"], False),   # not a Python launcher
])
def test_venv_redirector_classification(name, cmdline, below, redirector):
    """The venv launcher re-runs its own argv as a child (measured, argv[0]
    included), so it is recognised structurally, not by an m3 marker."""
    assert process.is_venv_redirector(name, cmdline, below) is redirector


# -- agent_client against a REAL process tree (same mechanism on every OS) ----

_CHILD = ("import os,sys; sys.path.insert(0, {bin!r}); "
          "from m3_core.process import agent_client; c = agent_client(); "
          "print(c.pid if c else 'NONE')")


def _run_child(via_m3_shell: bool) -> str:
    code = _CHILD.format(bin=str(_BIN))
    if not via_m3_shell:
        argv = [sys.executable, "-c", code]
        return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
    # A shell whose command line runs "m3" is part of m3's launch chain and must
    # be skipped. The trailing "m3" argument is what marks it.
    script = Path(__file__).with_name("_agent_client_child.py")
    script.write_text(code, encoding="utf-8")
    try:
        if os.name == "nt":
            argv = ["cmd", "/c", sys.executable, str(script), "m3"]
        else:
            argv = ["sh", "-c", f'"{sys.executable}" "{script}" m3']
        return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()
    finally:
        script.unlink(missing_ok=True)


def test_agent_client_is_the_nearest_non_m3_ancestor():
    pytest.importorskip("psutil")
    assert _run_child(via_m3_shell=False) == str(os.getpid())


def test_agent_client_skips_a_shell_that_launches_m3():
    pytest.importorskip("psutil")
    assert _run_child(via_m3_shell=True) == str(os.getpid())
