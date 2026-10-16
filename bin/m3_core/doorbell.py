"""Client for m3-dispatchd, the per-user doorbell for agent mail. Stdlib only.

Writers RING after committing a message; waiters WAIT for their readers and are
woken once per burst (docs/ROADMAP_DISPATCHER.md, "Implementation"). The daemon
is an accelerator: every call here returns None/False when it is not running,
and callers keep their existing behaviour (the waiter polls).

Stdlib only because the notification waiter imports it, and the waiter must
start without the rest of m3 (it survives `pipx upgrade` that way). The
endpoint comes from ``dispatch_endpoint`` in the OS seam (through ``m3_core.seams``).

Transport: HTTP/1.1 over a Unix socket, or over a Windows named pipe opened as
a file (Python has no AF_UNIX on Windows). No TLS, no tokens: the OS decides who
may connect (see the daemon's ``ipc`` module).
"""

from __future__ import annotations

import json
import os
import threading

RING_BUDGET_S = 0.25  # the most a writer waits on a ring, daemon up or not
_PIPE_PREFIX = "\\\\.\\pipe\\"


def _is_pipe(endpoint: str) -> bool:
    """A Windows named pipe, decided by the endpoint the OS seam gave, not by
    testing the OS here."""
    return endpoint.startswith(_PIPE_PREFIX)


def _http(method: str, path: str, body: dict | None) -> bytes:
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    head = (
        f"{method} {path} HTTP/1.1\r\nHost: m3\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode("ascii") + data


def _parse(raw: bytes) -> tuple[int, dict | None]:
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, (json.loads(payload) if payload.strip() else None)


def _request(endpoint: str, method: str, path: str, body: dict | None, timeout: float) -> tuple[int, dict | None]:
    """One request; raises OSError when the daemon is not reachable."""
    req = _http(method, path, body)
    if _is_pipe(endpoint):
        # A pipe opened as a file has no timeout; the caller bounds the time by
        # running this in a thread when it must (ring).
        from pathlib import Path

        with Path(endpoint).open("r+b", buffering=0) as f:
            f.write(req)
            raw = b""
            while chunk := f.read(65536):
                raw += chunk
        return _parse(raw)
    import socket

    af_unix = getattr(socket, "AF_UNIX", None)  # absent where the platform has no Unix sockets
    if af_unix is None:
        raise OSError(f"no Unix sockets here for endpoint {endpoint}")
    with socket.socket(af_unix, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(endpoint)
        s.sendall(req)
        raw = b""
        while chunk := s.recv(65536):
            raw += chunk
    return _parse(raw)


def ring(endpoint: str, recipients: list[str]) -> bool:
    """Tell the daemon messages for ``recipients`` were committed. Never raises
    and never holds a writer longer than RING_BUDGET_S; False when not delivered."""
    recipients = [r for r in recipients if r]
    if not endpoint or not recipients:
        return False
    if not _is_pipe(endpoint) and not os.path.exists(endpoint):
        return False  # no daemon: the cheap, common answer
    done: list[bool] = []

    def send() -> None:
        try:
            status, _ = _request(endpoint, "POST", "/ring", {"recipients": recipients[:1000]}, RING_BUDGET_S)
            done.append(status == 204)
        except (OSError, ValueError, IndexError):
            done.append(False)

    t = threading.Thread(target=send, daemon=True, name="m3-doorbell-ring")
    t.start()
    t.join(RING_BUDGET_S)
    return bool(done and done[0])


def wait(endpoint: str, readers: list[str], timeout_s: float) -> dict | None:
    """Block until mail for ``readers``, a resync, or the timeout. Returns the
    daemon's answer ({"wake": "mail"|"resync"|"timeout", ...}), or None when the
    daemon is not reachable (the caller falls back to polling)."""
    readers = [r for r in readers if r]
    if not endpoint or not readers:
        return None
    try:
        status, payload = _request(
            endpoint, "POST", "/wait", {"readers": readers, "timeout_s": timeout_s}, timeout_s + 10.0
        )
    except (OSError, ValueError, IndexError):
        return None
    return payload if status == 200 and isinstance(payload, dict) else None


def endpoint_for(engine_root: str) -> str | None:
    """The OS seam's endpoint for ``engine_root``, or None if the seam is not
    importable (a stripped payload): no doorbell then, never a guess."""
    try:
        from m3_core.seams import dispatch_endpoint
    except ImportError:
        return None
    return dispatch_endpoint(engine_root)
