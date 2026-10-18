"""m3 setup wires Google Antigravity where Antigravity reads it.

All locations come from one owner (m3_memory.antigravity):
- hooks: ``~/.gemini/config/hooks.json``, read by the app, the IDE and the CLI;
  m3 owns the block named "m3" (SessionStart and Stop, handlers directly);
- MCP: ``~/.gemini/config/mcp_config.json`` (``agy mcp list`` reads it; the CLI's
  settings.json is not read for MCP servers).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from m3_memory import antigravity, installer


@pytest.fixture
def agy_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(antigravity.shutil, "which", lambda name: None)
    monkeypatch.setattr("m3_memory._platform.local_app_data", lambda: None)  # agy's Windows install folder
    monkeypatch.setattr(installer, "_python_exe", lambda windowless=False: "/py/python")
    (tmp_path / ".gemini" / "antigravity-cli").mkdir(parents=True)  # the CLI is installed
    return tmp_path


def _block(home: Path) -> dict:
    return json.loads((home / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8"))


def test_hooks_go_in_the_m3_block_and_keep_other_blocks(agy_home):
    path = agy_home / ".gemini" / "config" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"user-lint": {"PostToolUse": [{"matcher": "x", "hooks": [{"command": "lint"}]}]}}))

    msg = installer._register_antigravity_hooks()
    assert msg and msg.startswith("[+]"), msg
    data = _block(agy_home)
    assert "user-lint" in data  # the user's block is kept
    (start,) = data["m3"]["SessionStart"]
    (stop,) = data["m3"]["Stop"]
    assert start["command"].endswith("antigravity-session-start.py") and "hooks" not in start
    assert stop["command"].endswith("antigravity-capture.py") and stop["timeout"] == 90
    assert installer._register_antigravity_hooks() is None  # converged


def test_hooks_are_not_written_when_antigravity_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(antigravity.shutil, "which", lambda name: None)
    monkeypatch.setattr("m3_memory._platform.local_app_data", lambda: None)
    assert installer._register_antigravity_hooks() is None
    assert not (tmp_path / ".gemini" / "config").exists()


def test_repair_only_leaves_a_file_without_the_m3_block(agy_home):
    assert installer._register_antigravity_hooks(repair_only=True) is None
    assert not (agy_home / ".gemini" / "config" / "hooks.json").exists()


def test_the_m3_plugin_carries_the_hooks_so_setup_does_not_add_a_second_set(agy_home):
    plugin = agy_home / ".gemini" / "antigravity-cli" / "plugins" / "m3"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps({"name": "m3"}))
    (plugin / "hooks.json").write_text("{}")
    assert installer._register_antigravity_hooks() is None


def test_an_m3_plugin_without_hooks_at_its_root_still_gets_setups_hooks(agy_home):
    """Antigravity loads a plugin's hooks.json from the plugin root only; an m3
    plugin staged without one captures nothing, so setup wires its own."""
    plugin = agy_home / ".gemini" / "antigravity-cli" / "plugins" / "m3"
    (plugin / ".antigravity-plugin").mkdir(parents=True)
    (plugin / ".antigravity-plugin" / "plugin.json").write_text(json.dumps({"name": "m3"}))
    (plugin / ".antigravity-plugin" / "hooks.json").write_text("{}")
    msg = installer._register_antigravity_hooks()
    assert msg and msg.startswith("[+]"), msg


def test_an_unreadable_hooks_file_is_reported_not_overwritten(agy_home):
    path = agy_home / ".gemini" / "config" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    msg = installer._register_antigravity_hooks()
    assert msg and msg.startswith("[!]") and "inspect" in msg
    assert path.read_text() == "{not json"


def test_windows_path_with_a_space_is_refused_loudly(agy_home, monkeypatch):
    """agy on Windows escapes the quotes a spaced path needs, so the hook would
    never run; setup says so instead of writing a line that silently fails."""
    from conftest import simulate_os

    simulate_os(monkeypatch, "Windows")
    monkeypatch.setattr(installer, "_python_exe", lambda windowless=False: "C:/Program Files/py/pythonw.exe")
    msg = installer._register_antigravity_hooks()
    assert msg and msg.startswith("[!]") and "space" in msg and "inspect" in msg
    assert not (agy_home / ".gemini" / "config" / "hooks.json").exists()


def test_mcp_registers_in_the_shared_mcp_config(agy_home, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        installer, "_heal_agent_settings", lambda p, **k: seen.setdefault("path", p) and "[+] registered"
    )
    assert installer._register_antigravity_mcp() == "[+] registered"
    assert seen["path"] == agy_home / ".gemini" / "config" / "mcp_config.json"


def test_transcripts_are_found_on_every_surface(agy_home):
    for surface, conv in (("app", "a1"), ("cli", "c1")):
        logs = antigravity.surface_dir(surface) / "brain" / conv / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript.jsonl").write_text("{}\n")
    found = [(s, c) for s, c, _p in antigravity.transcripts()]
    assert found == [("app", "a1"), ("cli", "c1")]
