"""Where the payload's `bin/` (and docs/, _assets/, examples/) lives, and putting
`bin/` on `sys.path`: the ONE owner.

The installed package reaches `bin/` modules (memory_core, m3_core.config, ...)
only through `sys.path`. About twenty sites did the insert themselves, with three
spellings of where `bin/` is; one of them missed the wheel layout entirely and
reported a present script as missing (2026-07-27). They all call this now, and a
seam-integrity test bans other `sys.path` inserts in `m3_memory/`.

Stdlib-only (plus m3_memory._roots, itself stdlib-only): it runs before anything
in `bin/` can be imported. `[paths] bin`/`docs`/`assets`/`examples` in
m3_config.toml are LOCATORS (they say where the code is), so they are read by the
stdlib roots reader, not the config package that lives in that code.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()


def payload_dir(component: str, config_key: str, must_contain: str | None = None) -> Path | None:
    """Resolve a payload component dir. Precedence:
    1. ``[paths] <config_key>`` in m3_config.toml if set and it exists (and, if
       must_contain is given, contains it)
    2. the wheel-shipped copy inside this package: m3_memory/<component>
    3. a dev-checkout sibling: walk up from this file for <component>/
    4. None
    """

    def _ok(base: Path) -> bool:
        return os.path.isdir(base) and (must_contain is None or os.path.exists(base / must_contain))

    from m3_memory import _roots

    chosen = _roots.payload_path(config_key)  # [paths] <key> in m3_config.toml
    if chosen:
        p = Path(chosen)
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
    """The bin/ directory (payload scripts + memory_bridge.py). ``[paths] bin`` overrides."""
    return payload_dir("bin", "bin", must_contain="memory_bridge.py")


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
