"""Process liveness, identity and ancestry — the single owner.

Three modules answered "is this PID alive?" independently and disagreed:
``m3_halt`` (OpenProcess; access denied = alive), ``version_drift`` (tasklist;
another user's process = dead) and ``m3_core.locking`` (tasklist; a substring
match, so PID 12 matched a running 1234). Each now imports from here, as does
``m3_halt``'s ancestor walk.

Stdlib only at import time; ``psutil`` is imported lazily.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath

from m3_core.seams import is_windows

type Pid = int

# Max difference (seconds) between a recorded and a live create_time for the
# two to be the SAME process. Tight enough to catch PID reuse, loose enough to
# absorb create_time read-rounding (~10 ms on Linux).
CREATE_TIME_MATCH_TOLERANCE_S = 0.1


def pid_is_alive(pid: Pid) -> bool:
    """True if a process with ``pid`` exists now. Cross-platform, no subprocess.

    Answers only "does *a* process with this pid exist" — PID reuse is not
    detected here; use :func:`pid_is_live_owner` with a recorded create_time
    for identity.

    Fails toward ALIVE when existence cannot be disproved: a process running
    elevated or as another user exists (access denied / PermissionError), and
    reading it as gone has reported live services as stopped.
    """
    if pid <= 0:
        return False
    if is_windows():
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED  # type: ignore[attr-defined, unused-ignore]
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True  # handle opened, exit code unreadable -> exists
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # signal 0: existence probe, nothing delivered
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except (OSError, OverflowError):
        return True  # cannot disprove existence
    return True


def proc_create_time(pid: Pid) -> float | None:
    """Process create time (epoch seconds), or None if it cannot be read."""
    try:
        import psutil
    except Exception:  # noqa: BLE001 -- psutil absent: identity unknown, not an error
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001 -- gone, or not ours to inspect
        return None


def pid_is_live_owner(
    pid: Pid,
    recorded_create_time: float | None,
    *,
    alive: Callable[[Pid], bool] = pid_is_alive,
    create_time: Callable[[Pid], float | None] = proc_create_time,
) -> bool:
    """True iff ``pid`` is alive AND is the process that recorded ``recorded_create_time``.

    Closes PID reuse and the Windows STILL_ACTIVE(259) exit-code ambiguity.
    ``recorded_create_time`` None (a record older than the field) falls back to
    liveness only; an unreadable live create_time is treated as the owner rather
    than reaping a live process. ``alive`` / ``create_time`` are injectable so a
    caller's own (patchable) probes are used.
    """
    if not alive(pid):
        return False
    if recorded_create_time is None:
        return True
    live_ct = create_time(pid)
    if live_ct is None:
        return True
    return abs(live_ct - recorded_create_time) <= CREATE_TIME_MATCH_TOLERANCE_S


def ancestor_pids(pid: Pid, *, max_depth: int = 24) -> Iterator[Pid]:
    """Yield ``pid``'s parent, grandparent, ... up to the top of the chain.

    Bounded by ``max_depth`` and a seen-set, so a cycle or recycled pid cannot
    spin. Without psutil only ``os.getppid()`` is yielded (best effort, never
    worse than one generation). A failed lookup ends the walk: the result is
    partial, never wrong.
    """
    try:
        import psutil
    except ImportError:
        try:
            yield os.getppid()
        except Exception:  # noqa: BLE001
            pass
        return
    seen = {pid}
    cur = pid
    for _ in range(max_depth):
        try:
            parent = psutil.Process(cur).ppid()
        except Exception:  # noqa: BLE001 -- gone/denied -> stop climbing
            return
        # pid 0 (System Idle) and a self-parent both mean "top of the chain".
        if not parent or parent in seen:
            return
        yield parent
        seen.add(parent)
        cur = parent


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientProcess:
    """The long-lived process an agent session runs in (Claude Code, Antigravity, ...)."""

    pid: Pid
    create_time: float | None
    name: str


_SHELLS = frozenset({"cmd", "sh", "bash", "zsh", "dash", "fish", "pwsh", "powershell"})
_PY_LAUNCHERS = frozenset({"python", "python3", "pythonw", "py", "uv", "uvx", "pipx"})
# m3's entry points as a command-line word: the launcher, `-m m3_memory[.cli]`, the bridge.
_M3_ENTRY_STEMS = frozenset({"m3", "m3_memory", "memory_bridge"})


def _runs_m3(cmdline: list[str]) -> bool:
    """True when a command line launches m3 (launcher, package, or MCP bridge).

    Arguments are split into words: a shell receives its whole command as ONE
    argument (`bash -c "m3 agent agent_list"`), so a per-argument check misses it.

    Each word must BE an entry point (stem ``m3`` / ``m3_memory`` /
    ``memory_bridge``, or a file inside the ``m3_memory`` package), never merely
    contain one: a substring test classed ``pytest tests/test_memory_bridge.py``
    as m3's launcher and returned the shell above it as the agent's client.
    """
    for word in " ".join(cmdline).lower().split():
        path = PurePath(word.strip("\"'"))
        if path.stem in _M3_ENTRY_STEMS or "m3_memory" in path.parts[:-1]:
            return True
    return False


def is_m3_launch_step(name: str, cmdline: list[str]) -> bool:
    """Is this process part of m3's OWN launch chain rather than the agent's client?

    Measured 2026-10-08 on Windows: m3 MCP server = python.exe <- m3.exe launcher
    <- claude.exe. The launcher dies with the server on every MCP reconnect, so
    it must be skipped; a shell or Python launcher counts only when its command
    line runs m3 (a Python-based client such as aider must not be skipped).
    """
    match PurePath(name.lower()).stem:
        case "m3":
            return True
        case stem if stem in _SHELLS or stem in _PY_LAUNCHERS:
            return _runs_m3(cmdline)
        case _:
            return False


def is_venv_redirector(name: str, cmdline: list[str], below: list[str] | None) -> bool:
    """Is this the venv launcher of the process directly below it?

    On Windows a venv's ``Scripts\\python.exe`` is a redirector: it starts the
    base interpreter as a CHILD process with its own argv, byte-identical
    (measured 2026-10-08, argv[0] included). So every venv Python, pipx
    installs included, has an extra python.exe above it that belongs to its own
    launch, not to whoever started it. The signature is structural: a Python
    launcher whose arguments equal the ones below it.

    uv's venv trampoline differs in argv[0] only: it starts the base interpreter
    with argv[0] rewritten to the base path (measured 2026-10-09, uv 0.12.17), so
    an exact match missed every uv-made venv (`uv tool install`, `uv venv`) and
    reported the trampoline as the agent's client. argv[0] may then differ when
    the launcher is itself a venv's interpreter (pyvenv.cfg beside its folder).
    """
    if below is None or not cmdline or len(cmdline) != len(below):
        return False
    if PurePath(name.lower()).stem not in _PY_LAUNCHERS or cmdline[1:] != below[1:]:
        return False
    return cmdline[0] == below[0] or _is_venv_interpreter(cmdline[0])


def _is_venv_interpreter(exe: str) -> bool:
    """Is ``exe`` a venv's own interpreter (<venv>/Scripts|bin/python, pyvenv.cfg in <venv>)?"""
    try:
        return (Path(exe).parent.parent / "pyvenv.cfg").is_file()
    except OSError:
        return False


def agent_client(pid: Pid | None = None, *, max_depth: int = 24) -> ClientProcess | None:
    """The nearest ancestor of ``pid`` (default: this process) outside m3's launch chain.

    That is the agent's client — it lives as long as the agent session, while
    m3's own processes restart on every MCP reconnect. None when psutil is
    absent or the chain cannot be read; callers then rely on activity time alone.
    A venv redirector layer (:func:`is_venv_redirector`) is part of the process
    below it and is skipped.
    """
    try:
        import psutil
    except ImportError:
        return None
    start = os.getpid() if pid is None else pid
    try:
        below: list[str] | None = psutil.Process(start).cmdline()
    except Exception:  # noqa: BLE001 -- cannot read our own start: no redirector check
        below = None
    for anc in ancestor_pids(start, max_depth=max_depth):
        try:
            p = psutil.Process(anc)
            name, cmdline = p.name(), p.cmdline()
        except Exception:  # noqa: BLE001 -- vanished or denied: cannot classify
            return None
        if not (is_venv_redirector(name, cmdline, below) or is_m3_launch_step(name, cmdline)):
            return ClientProcess(pid=anc, create_time=proc_create_time(anc), name=name)
        below = cmdline
    return None
