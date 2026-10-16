"""Where the m3-core-rs wheel's executables are (m3-embed-server, m3-dispatchd,
m3-wait). Stdlib and the OS seam only: the notification waiter imports this
before the rest of m3.

This install's wheel comes before PATH: asking PATH first ran an older
install's binary left in ~/.local/bin (measured 2026-10-15). The wheel is found
with ``find_spec``, so locating a binary never loads the native extension.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

from m3_memory._platform import executable_name


def core_binary(base: str) -> Path | None:
    """The executable ``base`` from the m3-core-rs wheel, else from PATH, else None."""
    name = executable_name(base)
    try:
        spec = importlib.util.find_spec("m3_core_rs")
    except (ImportError, ValueError):
        spec = None
    origin = spec.origin if spec else None
    if origin:
        wheel_dir = Path(origin).parent
        for candidate in (wheel_dir / name, wheel_dir / "bin" / name, wheel_dir.parent / name):
            if os.path.isfile(candidate):
                return candidate
    on_path = shutil.which(name)
    return Path(on_path) if on_path else None
