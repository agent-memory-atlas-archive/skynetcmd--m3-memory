"""Pure output helpers for the setup wizard (color, say/ok/warn/err, progress).

Extracted verbatim from setup_wizard.py. None of these are ever monkeypatched
by tests, and none of them call anything that is — safe to live in a
submodule and be imported back into setup_wizard.py at module load time.
"""

from __future__ import annotations

import os
import sys

from m3_memory._platform import is_windows

# Who launched this process: "upgrade" when `m3 upgrade` ran it with the top-level
# `m3 --caller upgrade <command>`. The upgrade verifies and summarizes, so those
# steps report only changes and problems. A flag, not env (user decision
# 2026-10-11, m3 c8b803b5; it was M3_SETUP_CALLER): m3_upgrade passes it only when
# the TARGET release lists it in `m3 --help`, so an older target, which would
# reject an unknown flag, just runs with its fuller output. m3 children of an
# upgraded step get it from _m3_cli (setup_wizard), not by inheriting env.
CALLER_FLAG = "--caller"
_caller: str | None = None


def set_caller(value: str | None) -> None:
    """Record who launched this process (cli.main, from --caller)."""
    global _caller
    _caller = value


def caller_args() -> list[str]:
    """The top-level flag an m3 child of this process needs, or []."""
    return [CALLER_FLAG, _caller] if _caller else []


def console_has_person() -> bool:
    """True when a person is at this console.

    On Windows, isatty() is not enough: the NUL device reports itself as a
    character device, so `< NUL` (how scripts and schedulers detach stdin) reads
    as a terminal. GetConsoleMode succeeds only for a real console input handle.
    bin/m3_upgrade.py keeps a copy (interactive_console) because it cannot
    import this package; a test pins the two together.
    """
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if not is_windows():
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
    handle = kernel32.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))  # STD_INPUT_HANDLE
    mode = wintypes.DWORD()
    return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def called_by_upgrade() -> bool:
    """True when this process runs on behalf of `m3 upgrade`."""
    return _caller == "upgrade"


def _color(code: str, msg: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return msg
    return f"\033[{code}m{msg}\033[0m"


def _say(msg: str) -> None:
    print(f"{_color('36', '==>')} {msg}", flush=True)


def _ok(msg: str) -> None:
    print(_color("32", f"[OK] {msg}"), flush=True)


def _warn(msg: str) -> None:
    print(_color("33", f"[!] {msg}"), flush=True)


def _err(msg: str) -> None:
    print(_color("31", f"[X] {msg}"), file=sys.stderr, flush=True)


# Transient single-line progress for long, line-by-line sequences (per-package
# installs, per-section embeds). On a TTY each call REWRITES the same line
# (carriage-return + clear-to-end-of-line) so a 20-line "installing X / installed
# X" wall collapses to one self-updating status line. When stdout is NOT a TTY
# (piped, redirected, non-interactive SSH, CI) it degrades to a normal newline
# print so logs stay complete and grep-able. Call once more with done=True (or
# follow with a plain _ok) to commit a final newline so the next output starts
# on its own line.
_PROGRESS_ACTIVE = False


def _progress(msg: str, *, done: bool = False) -> None:
    global _PROGRESS_ACTIVE
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        # Non-interactive: every step on its own line (full, parseable log).
        print(f"    {msg}", flush=True)
        return
    # Interactive: rewrite the current line. \r returns to col 0; \033[K clears
    # to end of line so a shorter message doesn't leave stale trailing chars.
    end = "\n" if done else ""
    sys.stdout.write(f"\r\033[K    {msg}{end}")
    sys.stdout.flush()
    _PROGRESS_ACTIVE = not done


def _progress_done() -> None:
    """Commit a newline if a transient progress line is still open (TTY only)."""
    global _PROGRESS_ACTIVE
    if _PROGRESS_ACTIVE and sys.stdout.isatty():
        sys.stdout.write("\n")
        sys.stdout.flush()
    _PROGRESS_ACTIVE = False
