"""DEPRECATED_ENV_RENAMES: the old (un-namespaced) names doctor names, to delete.

The map once had to match the getenv_compat(new, old) call sites, because the
shim read the old names. m3 reads neither spelling now and the shim is gone; the
map only tells doctor which old names to report. Kept invariants: it is pure M3_
namespacing, and no reader of the old names comes back.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1] / "bin"
_CALL = re.compile(r"\bgetenv_compat\(")


def test_no_getenv_compat_reader_remains():
    """The legacy-name shim is removed; a call would read an old env name again."""
    sites = [
        str(py.relative_to(_BIN))
        for py in _BIN.rglob("*.py")
        if "__pycache__" not in py.parts
        and py.name != "check_env_access.py"  # the env gate's own pattern table names it
        and _CALL.search(py.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not sites, f"getenv_compat call sites reappeared: {sites}"
    sys.path.insert(0, str(_BIN))
    import m3_sdk
    from m3_core import paths

    assert not hasattr(paths, "getenv_compat") and not hasattr(m3_sdk, "getenv_compat")


def test_rename_map_is_pure_namespacing():
    sys.path.insert(0, str(_BIN))
    from m3_core.paths import DEPRECATED_ENV_RENAMES

    # Every new name is the old name with an M3_ prefix (the migration's rule).
    for old, new in DEPRECATED_ENV_RENAMES.items():
        assert new == f"M3_{old}", f"{old} -> {new} is not pure M3_ namespacing"


def test_every_renamed_old_name_is_reported_by_doctor():
    """doctor's old-name report scans all_env_renames(); the map feeds it."""
    sys.path.insert(0, str(_BIN))
    from m3_core.paths import DEPRECATED_ENV_RENAMES, all_env_renames

    assert set(DEPRECATED_ENV_RENAMES) <= set(all_env_renames())
