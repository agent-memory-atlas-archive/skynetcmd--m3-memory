"""m3's MCP server writes nothing but protocol messages on stdout.

An agent reads every stdout line from the stdio server as a JSON-RPC message.
One stray line (a debug print, a library warning, a banner) and the agent drops
the connection or shows m3's tools as broken, mid-session. Logs belong on
stderr. This runs the real server the way an agent starts it (`m3`, no
arguments, stdin a pipe), against the test sandbox's store, through a handshake,
the tool list and calls from several domains, and fails on any non-protocol line.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time

import pytest

_START = "import sys; from m3_memory.cli import main; sys.argv = ['m3']; raise SystemExit(main())"
_REQUESTS = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "memory_write",
            "arguments": {"content": "stdio purity probe", "type": "note", "title": "probe"},
        },
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "memory_search", "arguments": {"query": "stdio purity probe", "k": 2}},
    },
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "chatlog_search", "arguments": {"query": "probe", "k": 2}},
    },
]
_WAIT_S = 180  # server start-up on a cold CI runner, plus the calls


def test_every_stdout_line_is_a_protocol_message():
    proc = subprocess.Popen(
        [sys.executable, "-c", _START],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines: queue.Queue[str] = queue.Queue()
    stderr_tail: list[str] = []
    readers = [
        threading.Thread(target=lambda: [lines.put(ln) for ln in proc.stdout], daemon=True),
        threading.Thread(target=lambda: [stderr_tail.append(ln) for ln in proc.stderr], daemon=True),
    ]
    for t in readers:
        t.start()
    try:
        for req in _REQUESTS:
            proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        wanted = {r["id"] for r in _REQUESTS if "id" in r}
        got: set[int] = set()
        stray: list[str] = []
        deadline = time.monotonic() + _WAIT_S
        while got != wanted and time.monotonic() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                stray.append(line.rstrip()[:200])
                continue
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                stray.append(line.rstrip()[:200])
            elif msg.get("id") in wanted:
                got.add(msg["id"])
        assert not stray, f"non-protocol lines on the MCP server's stdout: {stray}"
        if got != wanted:
            pytest.fail(
                f"the server answered {sorted(got)} of {sorted(wanted)} within {_WAIT_S}s; "
                f"stderr tail: {''.join(stderr_tail[-15:])}"
            )
    finally:
        proc.kill()
        proc.wait(timeout=30)
        for t in readers:
            t.join(timeout=10)  # they end at EOF, once the process is gone
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass  # the pipe already broke with the killed process
