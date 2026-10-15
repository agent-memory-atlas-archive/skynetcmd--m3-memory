"""The version and OS seams (and the roots owner), as bin/ reaches them. Re-exports only; no logic.

The seams have ONE owner each: m3_memory/_pyver.py (Python version) and
m3_memory/_platform.py (OS). bin/ runs in two layouts:
  * installed: bin/ is shipped inside the m3_memory package, so it imports;
  * a source checkout run with an interpreter that lacks the package
    (`python bin/gen_tool_inventory.py`, CI lint, the pre-push hook), where
    bin/.. is the repo root that contains m3_memory/.
This module makes m3_memory resolvable in the second layout, by APPENDING the
checkout root to sys.path (an installed package still wins), then re-exports the
seams' public names. bin/ code imports `from m3_core.seams import is_windows`.
"""

from __future__ import annotations

import importlib.util as _ilu
import os as _os
import sys as _sys
from pathlib import Path as _Path

if _ilu.find_spec("m3_memory") is None:
    _root = _Path(__file__).resolve().parents[2]
    # os.path for the probe: Path.is_file() raises on 3.12/3.13 under an
    # untraversable parent (m3_memory/_pyver.py).
    if _os.path.isfile(_root / "m3_memory" / "_platform.py"):
        _sys.path.append(str(_root))

from m3_memory import _childenv as childenv  # noqa: E402  (child-process env owner, stdlib-only)
from m3_memory import _root_migration as root_migration  # noqa: E402  (Root-1 migration, stdlib-only)
from m3_memory import _roots as roots  # noqa: E402  (the root-directory owner, stdlib-only)
from m3_memory._platform import (  # noqa: E402
    PLATFORM_OF,
    console_has_person,
    current_uid,
    detached_spawn_kwargs,
    hidden_window_kwargs,
    is_apple_silicon,
    is_linux,
    is_macos,
    is_windows,
    new_process_group_kwargs,
    os_name,
    owned_by_current_user,
    python_exe,
    read_key,
    reexec,
    user_env_get,
    user_env_snapshot,
    user_service_runtime_dir,
    vscode_user_dir,
    wait_for_any_key,
)
from m3_memory._pyver import PY, event_loop_timer_slack, wheel_python_tag  # noqa: E402

__all__ = [
    "PLATFORM_OF",
    "PY",
    "childenv",
    "console_has_person",
    "current_uid",
    "detached_spawn_kwargs",
    "event_loop_timer_slack",
    "hidden_window_kwargs",
    "is_apple_silicon",
    "is_linux",
    "is_macos",
    "is_windows",
    "new_process_group_kwargs",
    "os_name",
    "owned_by_current_user",
    "python_exe",
    "read_key",
    "reexec",
    "root_migration",
    "roots",
    "user_env_get",
    "user_env_snapshot",
    "user_service_runtime_dir",
    "vscode_user_dir",
    "wait_for_any_key",
    "wheel_python_tag",
]
