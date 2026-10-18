"""Plugin capture hooks and `chatlog hook-path`, the old capture command an
older m3 still answers.

The Claude plugin's line runs `m3 hook` and falls back to `/bin/sh "<hook-path
output>"` on an m3 without it. The Antigravity plugin runs `m3 hook` in
Antigravity's own hooks.json shape. hook-path must never swallow a failure
(`|| true`, a `/dev/null` fallback) and must print the .sh launcher on every OS.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import simulate_os, skip_reason

ROOT = Path(__file__).resolve().parents[1]


def _capture_commands(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cmds = []
    for event in ("PreCompact", "Stop"):
        for group in data["hooks"][event]:
            for h in group.get("hooks", []):
                cmds.append(h["command"])
    assert cmds, f"no capture hooks found in {path}"
    return cmds


def test_antigravity_plugin_runs_m3_hooks_in_antigravitys_shape():
    """Antigravity reads named blocks whose SessionStart and Stop take handlers
    directly (https://antigravity.google/docs/hooks/); each runs a hook that
    `m3 hook` knows."""
    from m3_memory.hook_launchers import HOOKS

    data = json.loads((ROOT / "plugins" / "antigravity" / "hooks.json").read_text(encoding="utf-8"))
    assert set(data) == {"m3"}
    block = data["m3"]
    assert set(block) == {"SessionStart", "Stop"}
    for event, name in (("SessionStart", "antigravity-session-start"), ("Stop", "antigravity-capture")):
        (handler,) = block[event]
        assert "hooks" not in handler and "matcher" not in handler
        assert handler["command"] == f"m3 hook {name}" and name in HOOKS


def _run(cmd: str, path_dirs: list[Path], tmp_path: Path) -> subprocess.CompletedProcess:
    sh = shutil.which("sh")
    env = dict(os.environ)
    # Only the fake bin plus the shell's own dir: a real mcp-memory must not run.
    env["PATH"] = os.pathsep.join([*(str(p) for p in path_dirs), str(Path(sh).parent)])
    return subprocess.run(
        [sh, "-c", cmd],
        input="{}",
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=tmp_path,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason=skip_reason("dep", "no POSIX sh on PATH"))


def _stub(folder: Path, name: str, body: str) -> None:
    exe = folder / name
    exe.write_bytes(f"#!/bin/sh\n{body}\n".encode())
    exe.chmod(0o755)


@needs_sh
def test_claude_plugin_capture_falls_back_on_an_older_m3(tmp_path):
    """Plugin files are served from main, so a plugin update can reach a user whose
    m3 predates `m3 hook`. That m3 rejects `hook` at argument parsing (exit 2,
    stdin unread); the line must then hand the SAME input to the old capture
    command, or capture silently stops. A current m3 runs the hook and its own
    exit code stands; a missing m3 says capture did not happen."""
    cmd = _capture_commands(ROOT / "hooks" / "claude.json")[0]
    script = tmp_path / "old_capture.sh"
    # An absolute target: the test's PATH holds only the stubs and sh's own folder,
    # and on macOS `dirname` lives in /usr/bin, not beside /bin/sh.
    script.write_bytes(f"cat > '{(tmp_path / 'got_stdin').as_posix()}'\nexit 0\n".encode())

    old = tmp_path / "old"
    old.mkdir()
    _stub(old, "m3", "echo \"m3: error: argument command: invalid choice: 'hook'\" >&2; exit 2")
    _stub(old, "mcp-memory", f"echo '{script.as_posix()}'")
    r = _run(cmd, [old], tmp_path)
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert (tmp_path / "got_stdin").read_text() == "{}", "the old capture did not receive the hook's input"

    new = tmp_path / "new"
    new.mkdir()
    _stub(new, "m3", "cat >/dev/null; echo captured-by-new >&2; exit 0")
    _stub(new, "mcp-memory", "echo SHOULD-NOT-RUN >&2; exit 9")
    r = _run(cmd, [new], tmp_path)
    assert r.returncode == 0 and "captured-by-new" in r.stderr and "SHOULD-NOT-RUN" not in r.stderr, r.stderr

    failing = tmp_path / "failing"
    failing.mkdir()
    _stub(failing, "m3", "cat >/dev/null; exit 1")
    _stub(failing, "mcp-memory", "echo SHOULD-NOT-RUN >&2; exit 9")
    r = _run(cmd, [failing], tmp_path)
    assert r.returncode == 1 and "SHOULD-NOT-RUN" not in r.stderr, "a real capture failure is not retried"

    empty = tmp_path / "empty"
    empty.mkdir()
    r = _run(cmd, [empty], tmp_path)
    assert r.returncode == 1 and "NOT captured" in r.stderr


def _hook_path(monkeypatch, tmp_path, capsys, os_name):
    from m3_memory import cli, installer

    bin_dir = tmp_path / "payload" / "bin"
    hooks = bin_dir / "hooks" / "chatlog"
    hooks.mkdir(parents=True)
    for ext in ("sh", "ps1", "py"):
        (hooks / f"claude_code_precompact.{ext}").write_text("#", encoding="utf-8")
    monkeypatch.setattr(installer, "find_bridge", lambda: bin_dir / "memory_bridge.py")
    simulate_os(monkeypatch, os_name)
    rc = cli._cmd_chatlog(argparse.Namespace(chatlog_cmd="hook-path", rest=[]))
    return rc, capsys.readouterr(), hooks


@pytest.mark.parametrize("os_name", ["Windows", "Darwin", "Linux"])
def test_hook_path_is_the_sh_launcher_on_every_os(monkeypatch, tmp_path, capsys, os_name):
    rc, out, hooks = _hook_path(monkeypatch, tmp_path, capsys, os_name)
    assert rc == 0
    assert out.out.strip() == (hooks / "claude_code_precompact.sh").as_posix()
    assert "\\" not in out.out, "a backslash path is mangled by the hook shell chain"


def test_hook_path_failure_is_diagnosable(monkeypatch, tmp_path, capsys):
    from m3_memory import cli, installer

    monkeypatch.setattr(installer, "find_bridge", lambda: None)
    rc = cli._cmd_chatlog(argparse.Namespace(chatlog_cmd="hook-path", rest=[]))
    err = capsys.readouterr()
    assert rc == 1 and err.out == ""
    assert "m3 install-m3" in err.err
