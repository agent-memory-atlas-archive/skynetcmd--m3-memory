"""Who is serving a local port: the one owner of that question.

A port that answers is not proof that OUR server is running. On a shared machine
another user's m3 (or another program) can hold the port, and a check that only
probed the port reported "running" and "verified healthy" while this account's
own server could not bind. ``held_by_another_owner`` is True only when the port
answers AND none of this user's processes listens on it.
"""

from __future__ import annotations

import socket

from m3_memory._platform import port_listener_is_mine


def _answers(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def first_free_port(start: int, span: int = 20, host: str = "127.0.0.1") -> int | None:
    """The first port in [start, start + span) that nothing answers on and that
    this process can bind, or None."""
    for port in range(start, min(start + span, 65536)):
        if _answers(port, host):
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
            return port
        except OSError:
            continue
    return None


def held_by_another_owner(port: int, host: str = "127.0.0.1") -> bool:
    """True when something answers on ``port`` but it is not one of this user's
    processes. False when nothing answers, when ours does, or when this cannot
    be told (no psutil): never a false alarm."""
    if not _answers(port, host):
        return False
    try:
        import psutil
    except ImportError:
        return False
    return port_listener_is_mine(port, psutil.process_iter(["pid"])) is False
