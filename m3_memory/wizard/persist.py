"""Shell-rc readers for the setup wizard.

The wizard no longer persists env vars (HKCU via setx, shell rc files, MCP
server env blocks): it records its choices in files every m3 process reads
(wizard cut-over, user decision 2026-10-11, m3 ec79a21b). What is left reads
the rc, to recognise a value the user already exports. setup_wizard.py
re-imports ``_pick_unix_shell_rc`` so tests can reach it as a setup_wizard
attribute.
"""

from __future__ import annotations

import os
from pathlib import Path

from m3_memory._platform import is_windows


def shell_rc_has(name: str, value: str) -> bool:
    """Whether the user's shell rc exports name=value as its LAST assignment of
    name (POSIX only). Commented-out lines do not count."""
    if is_windows():
        return False
    try:
        rc = _pick_unix_shell_rc()
        text = rc.read_text(encoding="utf-8") if os.path.exists(rc) else ""
    except OSError:
        return False
    prefix = f"export {name}="
    last = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(prefix):
            continue
        rhs = line[len(prefix) :]
        if rhs[:1] in ('"', "'") and rhs.count(rhs[0]) >= 2:
            rhs = rhs[1 : rhs.index(rhs[0], 1)]
        else:
            rhs = rhs.split("#", 1)[0].strip()
        last = rhs
    return last == value


def _pick_unix_shell_rc() -> Path:
    """Pick the shell rc file most likely to be read on this Unix system.

    Order:
      1. ~/.zshrc if SHELL points at zsh (macOS default since Catalina)
      2. ~/.bashrc if SHELL points at bash (most Linux distros)
      3. First existing among (~/.zshrc, ~/.bashrc, ~/.bash_profile, ~/.profile)
      4. Default to ~/.zshrc (covers fresh macOS Spotlight users)
    """
    home = Path.home()
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        return home / ".bashrc"
    for candidate in (home / ".zshrc", home / ".bashrc", home / ".bash_profile", home / ".profile"):
        if os.path.exists(candidate):
            return candidate
    return home / ".zshrc"
