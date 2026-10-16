"""Where the payload's `bin/` (and docs/, _assets/, examples/) lives, and putting
`bin/` on `sys.path`: the ONE owner.

The installed package reaches `bin/` modules (memory_core, m3_core.config, ...)
only through `sys.path`. About twenty sites did the insert themselves, with three
spellings of where `bin/` is; one of them missed the wheel layout entirely and
reported a present script as missing (2026-07-27). They all call this now, and a
seam-integrity test bans other `sys.path` inserts in `m3_memory/`.

Stdlib-only: it runs before anything in `bin/` can be imported. `M3_PATH_BIN` and
its siblings are bootstrap LOCATORS (they say where the code is, so they cannot be
read through the config package that lives in that code).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()


def payload_dir(component: str, env_var: str, must_contain: str | None = None) -> Path | None:
    """Resolve a payload component dir. Precedence:
    1. $<env_var> if set and exists (and, if must_contain is given, contains it)
    2. the wheel-shipped copy inside this package: m3_memory/<component>
    3. a dev-checkout sibling: walk up from this file for <component>/
    4. None
    """

    def _ok(base: Path) -> bool:
        return os.path.isdir(base) and (must_contain is None or os.path.exists(base / must_contain))

    env = os.environ.get(env_var)  # env-owner: the approved payload locator
    if env:
        p = Path(env).expanduser()
        if _ok(p):
            return p.resolve()
    here = Path(__file__).resolve()
    packaged = here.parent / component
    if _ok(packaged):
        return packaged
    for parent in here.parents:
        cand = parent / component
        if _ok(cand):
            return cand
    return None


def bin_dir() -> Path | None:
    """The bin/ directory (payload scripts + memory_bridge.py). $M3_PATH_BIN overrides."""
    # env-owner: the payload locator: it finds bin/, where the registry lives
    return payload_dir("bin", "M3_PATH_BIN", must_contain="memory_bridge.py")


def ensure_on_path(*, front: bool = False) -> Path | None:
    """Put bin/ on sys.path (once; idempotent and thread-safe) and return it.

    ``front=True`` also MOVES an existing entry to position 0, for hosts that
    must beat a shadowing top-level ``memory`` package (the Hermes integration).
    Returns None, changing nothing, when no payload bin/ exists: the caller then
    fails on its own import with the module it actually needed named.
    """
    d = bin_dir()
    if d is None:
        return None
    s = str(d)
    with _LOCK:
        if front and sys.path and sys.path[0] != s:
            sys.path[:] = [p for p in sys.path if p != s]
            sys.path.insert(0, s)
        elif s not in sys.path:
            sys.path.insert(0, s)
    return d
