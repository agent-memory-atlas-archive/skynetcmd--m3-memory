"""Chat capture for hosts whose hooks are code, not a command line: OpenCode and
OpenClaw. Both run JavaScript in the host, so m3 installs a few lines of it whose
only job is to start m3's hook line (m3_memory.hook_launchers.command_for) with
the event as JSON on stdin; the capture logic stays in Python.

- OpenCode loads every file in ``~/.config/opencode/plugins/`` at startup; m3's
  file runs capture on ``session.idle``, which fires when a turn finishes.
- OpenClaw loads managed hooks from ``~/.openclaw/hooks/<name>/`` (``HOOK.md``
  plus ``handler.js``); m3's runs on ``command:new``, ``command:reset`` and
  ``session:auto-reset``, the points where a session is retired, and is turned
  on with ``openclaw hooks enable``.

This module is the one owner of those locations and of the code m3 writes there.
Every path hangs off ``home()``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# The first line of each file m3 writes; it marks a file m3 may replace.
MARKER = "// m3 capture plugin"
OPENCODE_PLUGIN = "m3-capture.js"
OPENCLAW_HOOK = "m3-capture"
OPENCLAW_EVENTS = ("command:new", "command:reset", "session:auto-reset")
CAPTURE_TIMEOUT_MS = 90_000


def home() -> Path:
    return Path.home()


def opencode_plugin_path() -> Path:
    return home() / ".config" / "opencode" / "plugins" / OPENCODE_PLUGIN


def openclaw_hook_dir() -> Path:
    return home() / ".openclaw" / "hooks" / OPENCLAW_HOOK


def opencode_installed() -> bool:
    return shutil.which("opencode") is not None or (home() / ".config" / "opencode").is_dir()


def openclaw_binary() -> str | None:
    # The npm shim is openclaw.CMD on Windows; a subprocess needs the full path.
    return shutil.which("openclaw")


def _spawn_js(argv: list[str]) -> str:
    """JavaScript that starts ``argv`` with ``payload`` as JSON on stdin and does
    not wait for it: the host's turn never blocks on capture."""
    return f"""import {{ spawn }} from "node:child_process";

const M3_HOOK = {json.dumps(argv)};

function capture(payload) {{
  try {{
    const child = spawn(M3_HOOK[0], M3_HOOK.slice(1), {{
      stdio: ["pipe", "ignore", "ignore"],
      windowsHide: true,
      detached: false,
      timeout: {CAPTURE_TIMEOUT_MS},
    }});
    child.on("error", (e) => console.error(`m3 capture could not start: ${{e.message}}. repair: m3 doctor --fix`));
    child.stdin.end(JSON.stringify(payload));
  }} catch (e) {{
    console.error(`m3 capture could not start: ${{e.message}}. repair: m3 doctor --fix`);
  }}
}}
"""


def opencode_plugin_text(argv: list[str]) -> str:
    return f"""{MARKER}: OpenCode
// Installed by m3 setup and replaced on every m3 upgrade; edits here are overwritten.
// Saves each OpenCode session's turns to m3 when a turn finishes (session.idle).
{_spawn_js(argv)}
export const M3Capture = async ({{ directory }}) => ({{
  event: async ({{ event }}) => {{
    if (event.type !== "session.idle") return;
    capture({{
      session_id: event.properties?.sessionID ?? "",
      hook_event_name: "session.idle",
      cwd: directory ?? "",
    }});
  }},
}});
"""


def openclaw_hook_md() -> str:
    meta = {"openclaw": {"events": list(OPENCLAW_EVENTS)}}
    return f"""---
name: {OPENCLAW_HOOK}
description: "Save the session's turns to m3 when it is retired"
metadata: {json.dumps(meta)}
---

# m3 chat capture

Installed by m3 setup and replaced on every m3 upgrade. When a session is retired
(`/new`, `/reset`, or a daily or idle reset), it starts m3's capture hook, which
saves that session's turns to m3.
"""


def openclaw_handler_text(argv: list[str]) -> str:
    return f"""{MARKER}: OpenClaw
// Installed by m3 setup and replaced on every m3 upgrade; edits here are overwritten.
import {{ existsSync }} from "node:fs";
{_spawn_js(argv)}
export default function handler(event) {{
  const ctx = event?.context ?? event ?? {{}};
  const entry = ctx.previousSessionEntry ?? ctx.sessionEntry ?? {{}};
  // sessionFile may name a transcript without being a readable path; without
  // one, m3's hook reads the newest transcript under ~/.openclaw itself.
  const file = typeof entry.sessionFile === "string" && existsSync(entry.sessionFile) ? entry.sessionFile : "";
  capture({{
    session_id: entry.sessionId ?? "",
    transcript_path: file,
    hook_event_name: `${{event?.type ?? ""}}:${{event?.action ?? ""}}`,
    cwd: ctx.workspaceDir ?? "",
  }});
}}
"""


__all__ = [
    "CAPTURE_TIMEOUT_MS",
    "MARKER",
    "OPENCLAW_EVENTS",
    "OPENCLAW_HOOK",
    "OPENCODE_PLUGIN",
    "home",
    "openclaw_binary",
    "openclaw_handler_text",
    "openclaw_hook_dir",
    "openclaw_hook_md",
    "opencode_installed",
    "opencode_plugin_path",
    "opencode_plugin_text",
]
