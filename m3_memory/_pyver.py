"""Python-version differences: the one owner (the version seam; m3_memory._platform is its OS sibling).

Call sites ask for what they need ("the wheel tag for this interpreter", "how
early can an asyncio timer fire"); this module knows how each supported Python
(3.12 to 3.15) answers. It is the interpreter-version counterpart of the database
dialect: no `sys.version_info` check outside this file
(tests/test_python_version_seam.py), so raising the floor means reading one
module rather than grepping the tree.

Known differences that need no code today, recorded here so a floor raise finds
them:
  * argparse `deprecated=` is 3.13+; m3_core.paths._DeprecatedAlias emulates it.
  * itertools.batched gained `strict=` in 3.13; m3 calls it without, and the
    API-parity snapshot records signatures only for m3's own callables.
  * tomllib's TOMLDecodeError has no `lineno` on 3.12; the config loader reports
    a best-effort line instead.
  * sys.monitoring event VALUES differ (0x1005 on 3.12 vs 0x2005); m3_core.monitoring
    uses only the names.
  * zero-arg super() fails in a slots=True dataclass before 3.14; m3's slotted
    dataclasses do not call it.
  * pathlib's Path.exists()/is_file()/is_dir() RAISE PermissionError on 3.12 and
    3.13 when a parent directory cannot be traversed (POSIX); 3.14 returns False.
    Measured 2026-10-10 on Linux 3.12.13 / 3.13.15 / 3.14.5. os.path.exists /
    isfile / isdir return False on every version, so m3 uses those
    (tests/test_python_version_seam.py ratchets the Path forms down).
"""

from __future__ import annotations

import sys
import time

PY: tuple[int, int] = (sys.version_info.major, sys.version_info.minor)


def wheel_python_tag() -> str:
    """The CPython wheel tag for this interpreter, e.g. ``cp312``."""
    return f"cp{PY[0]}{PY[1]}"


def event_loop_timer_slack() -> float:
    """How much earlier than requested an asyncio timer can fire, measured
    with ``time.perf_counter``.

    asyncio schedules on ``time.monotonic``. On Windows before 3.13 that clock is
    GetTickCount64 with a 15.6 ms tick; 3.13 moved it to QueryPerformanceCounter.
    Two ticks of slack: asyncio's ``_run_once`` fires every timer due before
    ``now + clock_resolution`` (one tick), and a coarse clock reads up to one more
    tick off. Measured 2026-10-10 on 3.12.13: over 200 gathers, ``sleep(0.06)``
    ended up to 1.63 ticks early. The answer derives from the clock's reported
    resolution, so it is ~0 wherever the clock is fine, with no version test.
    """
    return 2 * time.get_clock_info("monotonic").resolution
