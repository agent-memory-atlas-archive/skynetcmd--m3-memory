"""Named launchers for m3's background jobs (macOS and Linux).

macOS lists a login item, and ``ps`` a process, by the program's name. Every m3
job ran as ``python``, so the login items read "python" seven times and could not
be told from debris. Each job now runs through a launcher named for it, declared
as a package entry point so pip and pipx recreate it on every install and
upgrade. A launcher does exactly what ``python [-u] SCRIPT ARGS...`` did.

Windows keeps ``pythonw.exe``: Task Scheduler shows the task's own name, and a
running ``.exe`` launcher would be locked against the upgrade that replaces it.
"""

from __future__ import annotations

import os
import runpy
import sys

# script in bin/ -> launcher name. pyproject's [project.scripts] declares each
# name (tests/test_job_launchers.py keeps the two in step).
JOB_LAUNCHERS: dict[str, str] = {
    "m3_cognitive_loop.py": "m3-cognitive-loop",
    "m3_loop_watchdog.py": "m3-loop-watchdog",
    "m3_notification_waiter.py": "m3-notification-waiter",
    "dashboard_server.py": "m3-dashboard",
    "embed_server_inproc.py": "m3-embed-inproc",
    "sync_all.py": "m3-sync",
    "secret_rotator.py": "m3-secret-rotator",
    "chatlog_embed_sweeper.py": "m3-chatlog-embed-sweep",
}


def launcher_for(python_exe: str, script: str) -> str:
    """The program a job definition should name: the job's launcher beside
    ``python_exe`` when this install has it, else ``python_exe`` itself (an
    install from before the launchers, or a script without one)."""
    name = JOB_LAUNCHERS.get(os.path.basename(script))
    if name:
        candidate = os.path.join(os.path.dirname(python_exe), name)
        if os.path.exists(candidate):
            return candidate
    return python_exe


def run_script() -> None:
    """Entry point of every launcher: ``LAUNCHER [-u] SCRIPT [ARGS...]``."""
    args = sys.argv[1:]
    if args and args[0] == "-u":  # python -u: unbuffered output
        args = args[1:]
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(line_buffering=True, write_through=True)
    if not args:
        sys.exit(f"usage: {os.path.basename(sys.argv[0])} [-u] SCRIPT [ARGS...]")
    script = os.path.abspath(args[0])
    sys.argv = [script, *args[1:]]
    # python puts the script's directory first; every job script lives in bin/,
    # which only its owner puts on the path.
    from m3_memory import _bin

    _bin.ensure_on_path(front=True)
    runpy.run_path(script, run_name="__main__")
