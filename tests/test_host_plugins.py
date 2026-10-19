"""OpenCode and OpenClaw chat capture: m3 setup installs a few lines of JavaScript
in each host (m3_memory.host_plugins) that start m3's hook line with the event as
JSON. The writers keep files that are not m3's, and the JavaScript is run under
Node to show it starts the hook with the right input.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import skip_reason
from m3_memory import host_plugins, installer


@pytest.fixture
def opencode_home(tmp_path, monkeypatch):
    monkeypatch.setattr(host_plugins, "home", lambda: tmp_path)
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    monkeypatch.setattr(installer, "_python_exe", lambda windowless=False: "/py/python")
    return tmp_path


@pytest.fixture
def openclaw_home(tmp_path, monkeypatch):
    monkeypatch.setattr(host_plugins, "home", lambda: tmp_path)
    monkeypatch.setattr(host_plugins, "openclaw_binary", lambda: "/bin/openclaw")
    monkeypatch.setattr(installer, "_python_exe", lambda windowless=False: "/py/python")
    calls: list[list[str]] = []

    def _run(argv, **_kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "enabled", "")

    monkeypatch.setattr(installer.subprocess, "run", _run)
    return tmp_path, calls


def test_opencode_plugin_runs_the_launcher_line(opencode_home):
    msg = installer._register_opencode_hooks()
    path = host_plugins.opencode_plugin_path()
    assert msg and msg.startswith("[+]") and path.is_file(), msg
    text = path.read_text(encoding="utf-8")
    assert text.startswith(host_plugins.MARKER)
    assert '"/py/python", "-E", "-s"' in text and "opencode-capture.py" in text
    assert installer._register_opencode_hooks() is None  # converged


def test_opencode_repair_only_never_adds_the_plugin(opencode_home):
    assert installer._register_opencode_hooks(repair_only=True) is None
    assert not host_plugins.opencode_plugin_path().exists()


def test_opencode_keeps_a_file_that_is_not_m3s(opencode_home):
    path = host_plugins.opencode_plugin_path()
    path.parent.mkdir(parents=True)
    path.write_text("export const Mine = async () => ({});\n", encoding="utf-8")
    msg = installer._register_opencode_hooks()
    assert msg and msg.startswith("[!]") and "not m3's file" in msg
    assert path.read_text(encoding="utf-8").startswith("export const Mine")


def test_opencode_not_installed_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(host_plugins, "home", lambda: tmp_path)
    monkeypatch.setattr(host_plugins.shutil, "which", lambda name: None)
    assert installer._register_opencode_hooks() is None
    assert not host_plugins.opencode_plugin_path().exists()


def test_openclaw_hook_is_written_and_enabled_once(openclaw_home):
    _home, calls = openclaw_home
    msg = installer._register_openclaw_hooks()
    d = host_plugins.openclaw_hook_dir()
    assert msg and msg.startswith("[+]"), msg
    assert (d / "handler.js").read_text(encoding="utf-8").startswith(host_plugins.MARKER)
    hook_md = (d / "HOOK.md").read_text(encoding="utf-8")
    for event in host_plugins.OPENCLAW_EVENTS:
        assert event in hook_md
    assert calls == [["/bin/openclaw", "hooks", "enable", host_plugins.OPENCLAW_HOOK]]
    # Current: nothing written, and the person's enabled/disabled choice is kept.
    assert installer._register_openclaw_hooks() is None
    assert len(calls) == 1


def test_openclaw_enable_failure_is_reported(openclaw_home, monkeypatch):
    monkeypatch.setattr(
        installer.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 2, "", "no gateway")
    )
    msg = installer._register_openclaw_hooks()
    assert msg and msg.startswith("[!]") and "no gateway" in msg and "hooks enable" in msg


def test_openclaw_keeps_a_handler_that_is_not_m3s(openclaw_home):
    _home, calls = openclaw_home
    d = host_plugins.openclaw_hook_dir()
    d.mkdir(parents=True)
    (d / "handler.js").write_text("export default () => {};\n", encoding="utf-8")
    msg = installer._register_openclaw_hooks()
    assert msg and "not m3's file" in msg and calls == []
    assert not (d / "HOOK.md").exists()


def test_openclaw_without_the_cli_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(host_plugins, "home", lambda: tmp_path)
    assert installer._register_openclaw_hooks() is None  # conftest hides the real binary
    assert not host_plugins.openclaw_hook_dir().exists()


# ── the JavaScript itself, under Node ────────────────────────────────────────

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason=skip_reason("dep", "node is not installed"))


def _recorder(out: Path) -> list[str]:
    """An argv that writes its stdin to ``out``, standing in for m3's hook line."""
    return [sys.executable, "-c", f"import sys, pathlib; pathlib.Path({str(out)!r}).write_text(sys.stdin.read())"]


def _run_node(module: Path, call: str) -> None:
    script = f"import({json.dumps(module.as_uri())}).then(async (m) => {{ {call} }});"
    r = subprocess.run([_NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


@needs_node
def test_opencode_plugin_starts_the_hook_on_session_idle(tmp_path):
    out = tmp_path / "stdin.json"
    module = tmp_path / "plugin.mjs"
    module.write_text(host_plugins.opencode_plugin_text(_recorder(out)), encoding="utf-8")
    _run_node(
        module,
        "const h = await m.M3Capture({ directory: '/work/site' });"
        "await h.event({ event: { type: 'message.updated', properties: {} } });"
        "await h.event({ event: { type: 'session.idle', properties: { sessionID: 'ses_1' } } });",
    )
    got = json.loads(out.read_text(encoding="utf-8"))
    assert got == {"session_id": "ses_1", "hook_event_name": "session.idle", "cwd": "/work/site"}


@needs_node
def test_openclaw_handler_passes_only_a_readable_transcript(tmp_path):
    out = tmp_path / "stdin.json"
    module = tmp_path / "handler.mjs"
    module.write_text(host_plugins.openclaw_handler_text(_recorder(out)), encoding="utf-8")
    real = tmp_path / "s1.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    for session_file, expected in ((str(real), str(real)), ("transcript-id-not-a-path", "")):
        event = {
            "type": "command",
            "action": "reset",
            "context": {
                "previousSessionEntry": {"sessionId": "s1", "sessionFile": session_file},
                "workspaceDir": "/ws",
            },
        }
        _run_node(module, f"m.default({json.dumps(event)});")
        got = json.loads(out.read_text(encoding="utf-8"))
        assert got == {
            "session_id": "s1",
            "transcript_path": expected,
            "hook_event_name": "command:reset",
            "cwd": "/ws",
        }, got
