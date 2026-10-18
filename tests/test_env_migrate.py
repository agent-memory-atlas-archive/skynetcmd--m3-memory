"""Old (un-namespaced) env names are NOT READ, and `m3 doctor` says so.

The namespacing migration once renamed old names (PG_URL, CHROMA_BASE_URL, ...)
to M3_* names that m3 read, and `doctor --fix` rewrote on-disk config to the new
spelling. m3 now reads neither spelling (settings come from m3_config.toml), so a
rename would only move an unread value, and the fixer was removed. What remains is
the report: `_deprecated_env_section` names each old name still set anywhere the
sweep looks (MCP env blocks, .env, rc files, the process, the Windows registry)
with where, to delete. The sandbox points that sweep at temp files only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from m3_memory import installer as I  # noqa: E402
from m3_memory.install import sections  # noqa: E402


def _mk(path: Path, servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_old_names_in_an_m3_env_block_are_named_with_their_file(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    _mk(
        settings,
        {"memory": {"command": "m3", "args": [], "env": {"CHROMA_BASE_URL": "http://x", "PG_URL": "postgres://y"}}},
    )
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    sections._deprecated_env_section()
    out = capsys.readouterr().out
    assert "old env names m3 does not read" in out
    assert "CHROMA_BASE_URL: set in" in out and "PG_URL: set in" in out
    assert str(settings) in out
    assert "postgres://y" not in out and "http://x" not in out, "values are never printed"


def test_a_clean_config_prints_nothing(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    _mk(settings, {"memory": {"command": "m3", "args": [], "env": {"M3_CONFIG_ROOT": str(tmp_path)}}})
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    sections._deprecated_env_section()
    assert capsys.readouterr().out == ""


def test_another_tools_entry_is_not_reported(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    _mk(settings, {"other-tool": {"command": "node", "args": ["x.js"], "env": {"PG_URL": "postgres://y"}}})
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    sections._deprecated_env_section()
    assert capsys.readouterr().out == ""


def test_malformed_json_is_skipped_not_raised(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    sections._deprecated_env_section()  # must not raise
    capsys.readouterr()


def test_an_old_name_in_the_process_is_named(monkeypatch, capsys):
    monkeypatch.setenv("ORIGIN_DEVICE", "box")
    sections._deprecated_env_section()
    out = capsys.readouterr().out
    assert "ORIGIN_DEVICE: set in environment" in out and "box" not in out


def test_the_report_never_rewrites_a_file(tmp_path, monkeypatch, capsys):
    """The rename fixer is gone: reporting leaves the file byte-identical."""
    settings = tmp_path / "settings.json"
    _mk(settings, {"memory": {"command": "m3", "args": [], "env": {"PG_URL": "postgres://y"}}})
    before = settings.read_bytes()
    monkeypatch.setattr(I, "_client_config_sources", lambda: {"Claude Code": [settings]})
    sections._deprecated_env_section()
    capsys.readouterr()
    assert settings.read_bytes() == before
    assert not hasattr(I, "_migrate_env_names"), "doctor --fix must not rename to another unread name"
