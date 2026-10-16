"""The plugin hooks (hooks/hooks.json, .antigravity-plugin/hooks.json) and the
`chatlog hook-path` they call.

They run `/bin/sh "<hook-path output>"` on every OS. Two defects lived here:
`|| true` plus a `/dev/null` fallback swallowed every failure (exit 0, nothing
said), and on Windows hook-path printed the .ps1, which /bin/sh cannot run.
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
HOOK_FILES = [ROOT / "hooks" / "hooks.json", ROOT / ".antigravity-plugin" / "hooks.json"]


def _capture_commands(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cmds = []
    for event in ("PreCompact", "Stop"):
        for group in data["hooks"][event]:
            for h in group.get("hooks", []):
                cmds.append(h["command"])
    assert cmds, f"no capture hooks found in {path}"
    return cmds


@pytest.mark.parametrize("path", HOOK_FILES, ids=lambda p: p.parent.name)
def test_capture_hooks_do_not_swallow_failure(path):
    for cmd in _capture_commands(path):
        assert "|| true" not in cmd, f"{path}: `|| true` hides every capture failure"
        assert "/dev/null)" not in cmd and "echo /dev/null" not in cmd, f"{path}: /dev/null fallback runs nothing"
        assert "hook-path" in cmd and 'exec /bin/sh "$hook"' in cmd


def _fake_bin(tmp_path: Path, hook_path_body: str) -> Path:
    b = tmp_path / "fakebin"
    b.mkdir()
    exe = b / "mcp-memory"
    exe.write_bytes(f"#!/bin/sh\n{hook_path_body}\n".encode())
    exe.chmod(0o755)
    return b


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


@needs_sh
@pytest.mark.parametrize("path", HOOK_FILES, ids=lambda p: p.parent.name)
def test_hook_command_exit_codes(path, tmp_path):
    cmd = _capture_commands(path)[0]

    # 1. CLI absent -> non-zero and says capture did not happen.
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _run(cmd, [empty], tmp_path)
    assert r.returncode == 1 and "NOT captured" in r.stderr, (r.returncode, r.stderr)

    # 2. hook-path fails -> non-zero, not a silent `/bin/sh /dev/null`.
    fail = tmp_path / "f"
    fail.mkdir()
    r = _run(cmd, [_fake_bin(fail, "echo 'no payload' >&2; exit 1")], tmp_path)
    assert r.returncode == 1 and "hook-path" in r.stderr and "no payload" in r.stderr, r.stderr

    # 3. The hook's own exit code reaches the host.
    ok = tmp_path / "o"
    ok.mkdir()
    script = ok / "hook.sh"
    script.write_bytes(b"cat >/dev/null\necho hook-ran >&2\nexit 7\n")
    r = _run(cmd, [_fake_bin(ok, f"echo '{script.as_posix()}'")], tmp_path)
    assert r.returncode == 7 and "hook-ran" in r.stderr, (r.returncode, r.stderr)


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
