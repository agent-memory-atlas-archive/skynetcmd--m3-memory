"""Touch ID for sudo — a tip on a Mac that could use it.

Report-only, never bumps the exit code: a password prompt is a supported
choice. Prints nothing off macOS. The offer during setup is made only at the
Mac, so a Mac installed or upgraded over SSH (or by a piped install script)
never saw it; this is where such a user learns of it.
"""

from __future__ import annotations


def run(brief: bool = False) -> int:
    try:
        from m3_memory import touchid
    except ImportError:
        return 0
    if not touchid._is_mac():
        return 0
    state = touchid.status()
    text = touchid._STATUS_TEXT[state]
    if brief:
        if state == "available":
            print(f"touchid: {text}")
        return 0
    print()
    print("=== Touch ID for sudo ===")
    print(f"  {text}")
    return 0
