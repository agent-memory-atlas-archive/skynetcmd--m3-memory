"""Agent settings JSON files (Claude Code's ``~/.claude/settings.json`` and the
other hosts' MCP settings): the one strict reader m3's rewriters share, and the
m3 plugin's MCP server ids.

Hazard: a reader that maps a malformed file to ``{}`` hands the writer an empty
dict, and the write then REPLACES the user's whole file (hooks, permissions,
other servers). Every m3 path that rewrites one of these files reads it here and
refuses on :class:`SettingsUnreadable`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# The m3 plugin's MCP server ids, as Claude Code spells them in
# ``disabledMcpServers``. ``memory`` is the current plugin server key
# (mcp__plugin_m3_memory__); ``m3`` is the pre-rename key an upgrader may carry.
# m3 writes both when it disables the plugin server.
PLUGIN_SERVER_IDS: tuple[str, ...] = ("plugin:m3:memory", "plugin:m3:m3")
# The id that disables the CURRENT plugin's server. Disabling only the legacy
# key leaves a current plugin's server live, so "is it disabled?" asks this one.
PLUGIN_SERVER_ID: str = PLUGIN_SERVER_IDS[0]


class SettingsUnreadable(Exception):
    """A settings file exists but cannot be used as a JSON object."""

    def __init__(self, path: str | os.PathLike[str], cause: str) -> None:
        self.path = os.fspath(path)
        self.cause = cause
        super().__init__(
            f"{self.path} {cause}; m3 did not rewrite it. Fix or move the file aside, then re-run the command."
        )


def read_json_object(path: str | os.PathLike[str]) -> dict:
    """The JSON object in ``path``; ``{}`` when the file is absent or empty.

    Raises :class:`SettingsUnreadable` (naming the path and the parse error)
    when it exists but cannot be read, is not valid JSON, or is not an object.
    """
    if not os.path.exists(path):
        return {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsUnreadable(path, f"cannot be read ({type(exc).__name__}: {exc})") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsUnreadable(path, f"is not valid JSON (line {exc.lineno} column {exc.colno}: {exc.msg})") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SettingsUnreadable(path, f"holds a JSON {type(data).__name__}, not an object")
    return data


def plugin_server_disabled(settings: dict) -> bool:
    """Is the current m3 plugin's MCP server listed in ``disabledMcpServers``?"""
    disabled = settings.get("disabledMcpServers")
    return isinstance(disabled, list) and PLUGIN_SERVER_ID in disabled
