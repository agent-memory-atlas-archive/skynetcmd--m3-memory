"""Where Google Antigravity keeps what m3 reads and writes: the one owner.

Antigravity ships as an app, an IDE and a CLI (``agy``). They share one global
config folder and one hook file, and each keeps its own app-data folder with a
transcript per conversation:

- ``~/.gemini/config/hooks.json``      hooks for every surface (SessionStart, Stop, ...)
- ``~/.gemini/config/mcp_config.json``  MCP servers for the CLI and the IDE
  (``agy mcp list`` reads it; the CLI's settings.json is not read for MCP servers)
- ``~/.gemini/antigravity-cli/settings.json``  the CLI's other settings
- ``<app dir>/brain/<conversationId>/.system_generated/logs/transcript.jsonl``,
  which cuts long messages short, and ``transcript_full.jsonl`` beside it with
  the same steps in full (read when present; the stdlib-only capture hook,
  bin/hooks/chatlog/antigravity_stop.py, carries its own copy of that name)

Reference: https://antigravity.google/docs/hooks/ . Stdlib only, so the
installer, the hooks and doctor can all import it.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

# The app-data folder of each surface, by the name m3 shows for it.
SURFACES: dict[str, str] = {"app": "antigravity", "ide": "antigravity-ide", "cli": "antigravity-cli"}

TRANSCRIPT_TAIL = (".system_generated", "logs", "transcript.jsonl")
FULL_TRANSCRIPT = "transcript_full.jsonl"


def gemini_home() -> Path:
    return Path.home() / ".gemini"


def config_dir() -> Path:
    return gemini_home() / "config"


def hooks_path() -> Path:
    return config_dir() / "hooks.json"


def mcp_config_path() -> Path:
    """MCP servers for the CLI and the IDE (``mcpServers`` schema)."""
    return config_dir() / "mcp_config.json"


def surface_dir(surface: str) -> Path:
    return gemini_home() / SURFACES[surface]


def installed_surfaces() -> list[str]:
    """The surfaces with an app-data folder on this machine, in SURFACES order."""
    return [s for s in SURFACES if os.path.isdir(surface_dir(s))]


def agy_binary() -> str | None:
    """The ``agy`` CLI: on PATH, or where its installer puts it."""
    found = shutil.which("agy")
    if found:
        return found
    from m3_memory._platform import local_app_data

    candidates = [Path.home() / ".local" / "bin" / "agy"]
    local = local_app_data()  # Windows: <local app data>/agy/bin/agy.exe
    if local:
        candidates.append(Path(local) / "agy" / "bin" / "agy.exe")
    return next((str(c) for c in candidates if os.path.isfile(c)), None)


def installed() -> bool:
    """True when any Antigravity surface is on this machine."""
    return bool(agy_binary() or installed_surfaces())


def full_transcript(path: Path) -> Path:
    """The untruncated sibling of a transcript.jsonl when it exists, else ``path``."""
    full = path.with_name(FULL_TRANSCRIPT)
    return full if os.path.isfile(full) else path


def transcripts() -> list[tuple[str, str, Path]]:
    """Every conversation transcript on disk: (surface, conversation id, path of
    transcript.jsonl; read it through ``full_transcript``)."""
    out: list[tuple[str, str, Path]] = []
    for surface in installed_surfaces():
        brain = surface_dir(surface) / "brain"
        if not os.path.isdir(brain):
            continue
        for conv in sorted(brain.iterdir()):
            path = conv.joinpath(*TRANSCRIPT_TAIL)
            if os.path.isfile(path):
                out.append((surface, conv.name, path))
    return out


def plugin_dirs() -> list[Path]:
    """Folders an installed Antigravity plugin can live in (the docs name both)."""
    return [config_dir() / "plugins", surface_dir("cli") / "plugins"]


def m3_plugin_installed() -> bool:
    """True when an installed m3 plugin carries m3's hooks: a plugin folder whose
    ``plugin.json`` is named "m3" and whose root holds the ``hooks.json``
    Antigravity loads. A plugin without that file captures nothing, so setup must
    still wire its own hooks."""
    for base in plugin_dirs():
        if not os.path.isdir(base):
            continue
        for d in base.iterdir():
            for manifest in (d / "plugin.json", d / ".antigravity-plugin" / "plugin.json"):
                if not os.path.isfile(manifest):
                    continue
                try:
                    named_m3 = json.loads(manifest.read_text(encoding="utf-8")).get("name") == "m3"
                except (OSError, ValueError, AttributeError):
                    continue
                if named_m3 and os.path.isfile(d / "hooks.json"):
                    return True
    return False
