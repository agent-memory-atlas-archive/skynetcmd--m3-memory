"""Is this machine running the newest m3? One small check, shared by every agent.

The newest version is asked of PyPI at most once a day and kept in a state file in
the engine root (``version_check.json``: when it was checked, what is installed,
what PyPI offers, and any error). Every agent's session-start hook only READS that
file, so no session waits on the network and N agents cost one request a day.

The check runs only when ``updates.check`` is on (setup asks, default yes). When
it is off, the hooks say so, so the user knows update alerts are theirs to watch.
This module is also the one owner of "the newest version on PyPI" and of how two
m3 versions compare (``m3 update``'s nudge and ``m3 upgrade`` ask it).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PYPI_URL = "https://pypi.org/pypi/m3-memory/json"
STATE_NAME = "version_check.json"
MAX_AGE = timedelta(hours=24)
UPGRADE_COMMAND = "m3 upgrade"


def version_key(v: str) -> tuple:
    """Sort key for a version string: packaging when importable, else the leading
    numeric release (a string sort puts 3.9 above 3.10)."""
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return (1, Version(v))
        except InvalidVersion:
            pass
    except ImportError:
        pass
    nums = re.match(r"\d+(?:\.\d+)*", v or "")
    return (0, tuple(int(p) for p in nums.group(0).split(".")) if nums else ())


def is_newer(candidate: str | None, installed: str | None) -> bool:
    """True when ``candidate`` is a newer release than ``installed``."""
    if not candidate or not installed:
        return False
    a, b = version_key(candidate), version_key(installed)
    return a[0] == b[0] and a > b


def installed_version() -> str | None:
    """The m3-memory version this interpreter imports (package metadata)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("m3-memory")
    except PackageNotFoundError:
        return None


def fetch_latest(timeout: float = 2.0) -> str:
    """The newest m3-memory version PyPI serves right now, uncached. Raises
    OSError/ValueError/KeyError when PyPI cannot be asked or answers oddly."""
    import urllib.request

    req = urllib.request.Request(PYPI_URL, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 — fixed https URL
        return str(json.load(r)["info"]["version"])


def enabled() -> bool:
    """``updates.check`` from m3_config.toml (registry default: off until setup asks)."""
    from m3_core.config.settings import setting

    return bool(setting("updates.check"))


def state_path() -> Path:
    from m3_core.paths import get_m3_engine_root

    return Path(get_m3_engine_root()) / STATE_NAME


def read_state() -> dict[str, Any] | None:
    """The last check's record, or None when there is none or it does not parse."""
    try:
        with state_path().open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def refresh(timeout: float = 2.0) -> dict[str, Any]:
    """Ask PyPI now and record the result (success or the error), atomically."""
    from m3_core.atomic_io import atomic_write
    from m3_core.runtime import utc_iso

    state: dict[str, Any] = {"checked_at": utc_iso(), "installed": installed_version(), "latest": None, "error": None}
    try:
        state["latest"] = fetch_latest(timeout)
    except (OSError, ValueError, KeyError) as e:
        state["error"] = f"{type(e).__name__}: {e}"[:300]
    atomic_write(state_path(), json.dumps(state, indent=2) + "\n")
    return state


def is_stale(state: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    """True when there is no record, it cannot be dated, or it is a day old."""
    if not state or not state.get("checked_at"):
        return True
    try:
        at = datetime.fromisoformat(str(state["checked_at"]))
    except ValueError:
        return True
    return (now or datetime.now(UTC)) - at >= MAX_AGE


def refresh_in_background() -> bool:
    """Start a detached child that refreshes the record, so a hook never waits.
    True when the child started."""
    from m3_core.seams import detached_spawn_kwargs

    code = "import sys; sys.path.insert(0, sys.argv[1]); from m3_core.version_check import refresh; refresh(timeout=10)"
    bin_dir = str(Path(__file__).resolve().parent.parent)
    try:
        subprocess.Popen(
            [sys.executable, "-c", code, bin_dir],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detached_spawn_kwargs(),
        )
    except OSError:
        return False
    return True


def _age(checked_at: str, now: datetime) -> str:
    try:
        at = datetime.fromisoformat(checked_at)
    except ValueError:
        return "at an unknown time"
    hours = int((now - at).total_seconds() // 3600)
    return "under an hour ago" if hours < 1 else f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


def status_line(*, now: datetime | None = None, background: bool = True) -> str:
    """One line for a person: the running version and what is known about newer
    ones. Reads the record only; when checking is on and the record is stale it
    starts a background refresh (``background``) and shows the last known result."""
    now = now or datetime.now(UTC)
    installed = installed_version() or "unknown"
    try:
        on = enabled()
    except Exception as e:  # noqa: BLE001 -- a status line must not fail a session
        return f"m3 {installed} · version check unknown ({type(e).__name__}). inspect: m3 config show"
    if not on:
        return (
            f"m3 {installed} · version checking is off — you won't hear about updates. "
            "turn on: `m3 config set updates.check true`"
        )
    state = read_state()
    if background and is_stale(state, now=now):
        refresh_in_background()
    if not state or not state.get("checked_at"):
        return f"m3 {installed} · checking for updates (result at the next session start)"
    if state.get("error") and not state.get("latest"):
        return f"m3 {installed} · last update check failed {_age(state['checked_at'], now)} ({state['error']})"
    latest = state.get("latest")
    if is_newer(latest, installed):
        return f"m3 {installed} · ⬆ {latest} is available — upgrade: `{UPGRADE_COMMAND}`"
    return f"m3 {installed} · up to date (checked {_age(state['checked_at'], now)})"
