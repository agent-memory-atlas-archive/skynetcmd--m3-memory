"""The dispatch doorbell: is m3-dispatchd serving this engine root, and who waits on it.

Report-only. Without the daemon the notification waiter polls, which is a
supported state, so this never fails the doctor. It speaks up when the core
ships the daemon but nothing answers: the waiter starts it, so a silent
endpoint usually means the waiter is not running either.
"""

from __future__ import annotations

import os


def run(brief: bool = False) -> int:
    try:
        from m3_core import doorbell
        from m3_core.paths import get_m3_engine_root, get_m3_logs_root
        from m3_core.seams import core_bins
    except Exception as e:  # noqa: BLE001 — report what blocked the probe, never crash the doctor
        print(f"dispatch: probe unavailable ({type(e).__name__}: {e})")
        return 0
    daemon = core_bins.core_binary("m3-dispatchd")
    waiter = core_bins.core_binary("m3-wait")
    if daemon is None:
        if not brief:
            print()
            print("=== Dispatch doorbell ===")
            print("  this core ships no m3-dispatchd: the notification waiter polls (supported)")
        return 0
    endpoint = doorbell.endpoint_for(str(get_m3_engine_root()))
    view = doorbell.status(endpoint)
    log = os.path.join(get_m3_logs_root(), "m3-dispatchd.log")
    if view is None:
        print(
            f"dispatch: m3-dispatchd is not answering on {endpoint}. observed: the core has it ({daemon}). "
            "possible: the notification waiter is not running (it starts the daemon). "
            f"inspect: `m3 doctor` job checks for the waiter, and {log}"
        )
        return 0
    readers = view.get("readers") or {}
    waiting = sum(1 for r in readers.values() if r.get("waiting"))
    if brief:
        print(f"dispatch: serving; {len(readers)} reader set(s), {waiting} waiting")
        return 0
    print()
    print("=== Dispatch doorbell ===")
    print(f"  m3-dispatchd: serving {endpoint}  ({daemon})")
    print(f"  m3-wait: {waiter or 'not in this core (the waiter blocks on the doorbell itself)'}")
    print(f"  rings logged: {view.get('rings_logged', 0)}   reader sets: {len(readers)}   waiting now: {waiting}")
    for key, r in sorted(readers.items()):
        pend = r.get("pending", 0)
        oldest = r.get("oldest_pending_ms")
        age = f", oldest {oldest / 1000:.1f}s" if pend and oldest is not None else ""
        print(f"    {key}: pending {pend}{age}, waiting {r.get('waiting', 0)}, wakes {r.get('wakes', 0)}")
    return 0
