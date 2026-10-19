"""m3's hooks, as one list: the launchers in ``~/.m3/hooks``, the line each agent's
config holds, and ``m3 hook <name>``.

An agent config holds ONE line per hook: the m3 environment's interpreter and a
launcher in the hooks folder (``m3_memory._roots.hooks_root``). The launcher is a
few lines that ask Python where m3 is installed (``import m3_memory``) and run
that version's hook script. So:

- a fix ships by replacing m3's files: setup and every upgrade rewrite the
  folder, and the agent's config, which also holds the user's and other tools'
  entries, is never edited for it;
- the line stays valid when the package moves (a new Python under pipx changes
  site-packages, not the interpreter path).

This module is the one owner of the hook list. The config writers, the
launchers and the CLI all read ``HOOKS``.
"""

from __future__ import annotations

import runpy
import sys
from dataclasses import dataclass
from pathlib import Path

# The first line of every launcher; it marks a file m3 may replace or remove.
MARKER = "# m3 hook launcher"


@dataclass(frozen=True, slots=True)
class Hook:
    script: str  # path under the payload's bin/, forward slashes
    args: tuple[str, ...] = ()


HOOKS: dict[str, Hook] = {
    "claude-session-start": Hook("hooks/session_start.py", ("--agent", "claude")),
    "claude-capture": Hook("hooks/chatlog/claude_code_precompact.py"),  # Stop and PreCompact
    "claude-session-end": Hook("hooks/session_end.py", ("--agent", "claude")),
    "codex-session-start": Hook("hooks/session_start.py", ("--agent", "codex")),
    "codex-capture": Hook("hooks/chatlog/codex_stop.py"),  # Stop
    "codex-session-end": Hook("hooks/session_end.py", ("--agent", "codex")),
    "gemini-session-start": Hook("hooks/session_start.py", ("--agent", "gemini")),
    "gemini-capture": Hook("hooks/chatlog/gemini_cli_onexit.py"),  # AfterModel, PreCompress, SessionEnd
    "antigravity-session-start": Hook("hooks/session_start.py", ("--agent", "antigravity")),
    "antigravity-capture": Hook("hooks/chatlog/antigravity_stop.py"),  # Stop
    "opencode-capture": Hook("hooks/chatlog/opencode_session_end.py"),
    "openclaw-capture": Hook("hooks/chatlog/openclaw_session_end.py"),
}


def hooks_dir() -> Path:
    from m3_memory._roots import hooks_root

    return Path(hooks_root())


def launcher_path(name: str) -> Path:
    return hooks_dir() / f"{name}.py"


def launcher_text(name: str) -> str:
    """The launcher for ``name``: stdlib only, so it reports a broken install
    instead of crashing on an import of its own."""
    hook = HOOKS[name]
    parts = ", ".join(repr(p) for p in hook.script.split("/"))
    return f"""{MARKER}: {name}
# Installed by m3 setup and replaced on every m3 upgrade; edits here are overwritten.
# It runs the hook of the m3 that this interpreter has installed, so fixes ship
# with m3 releases and the agent config line that points here never changes.
import json
import runpy
import sys
from pathlib import Path

try:
    from m3_memory._bin import bin_dir
except ImportError as e:
    print(json.dumps({{"systemMessage": f"m3 hook {name}: m3 is not installed for {{sys.executable}} ({{e}}). repair: m3 doctor --fix"}}))
    sys.exit(1)
_bin = bin_dir()
if _bin is None:
    print(json.dumps({{"systemMessage": f"m3 hook {name}: the m3 install has no bin/ folder. repair: m3 reinstall"}}))
    sys.exit(1)
script = Path(_bin).joinpath({parts})
sys.argv = [str(script), *{list(hook.args)!r}, *sys.argv[1:]]
runpy.run_path(str(script), run_name="__main__")
"""


def install_launchers() -> list[str]:
    """Write every launcher whose content differs and remove m3 launchers no longer
    listed (files carrying ``MARKER`` only; anything else in the folder is the
    user's). Returns one line per change, empty when everything was current."""
    from m3_memory import _bin

    _bin.ensure_on_path()
    from m3_core.atomic_io import atomic_write

    d = hooks_dir()
    d.mkdir(parents=True, exist_ok=True)
    changes: list[str] = []
    for name in HOOKS:
        p = launcher_path(name)
        text = launcher_text(name)
        try:
            current = p.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != text:
            atomic_write(p, text)
            changes.append(f"{'updated' if current is not None else 'wrote'} {p}")
    for p in sorted(d.glob("*.py")):
        if p.stem in HOOKS:
            continue
        try:
            ours = p.read_text(encoding="utf-8").startswith(MARKER)
        except OSError:
            ours = False
        if ours:
            p.unlink()
            changes.append(f"removed {p} (no longer an m3 hook)")
    return changes


def _quote(path: str) -> str:
    p = path.replace("\\", "/")
    return f'"{p}"' if " " in p else p


# Python flags for every hook process: ignore PYTHON* variables (-E) and the
# user site folder (-s), so a project's PYTHONPATH cannot make a hook import that
# project's code in place of m3's. Not -I: it also drops the script's own folder,
# which the hooks import from.
ISOLATE_FLAGS = ("-E", "-s")


def command_for(name: str, python: str) -> str:
    """The one line an agent's config holds for hook ``name``."""
    return f"{_quote(python)} {' '.join(ISOLATE_FLAGS)} {_quote(str(launcher_path(name)))}"


def argv_for(name: str, python: str) -> list[str]:
    """``command_for`` as an argument list, for a host that starts the hook from
    its own code (m3_memory.host_plugins) rather than a shell line."""
    return [python.replace("\\", "/"), *ISOLATE_FLAGS, str(launcher_path(name)).replace("\\", "/")]


def is_launcher_command(cmd: object) -> bool:
    """True when ``cmd`` runs one of m3's launchers (so a writer replaces it in place)."""
    if not isinstance(cmd, str):
        return False
    folder = str(hooks_dir()).replace("\\", "/")
    return folder in cmd.replace("\\", "/")


def run(name: str, argv: list[str] | None = None) -> int:
    """Run hook ``name`` in this process (``m3 hook <name>``, for plugins that
    cannot know where the hooks folder is)."""
    from m3_memory._bin import bin_dir

    hook = HOOKS[name]
    root = bin_dir()
    if root is None:
        print(f"m3 hook {name}: the m3 install has no bin/ folder. repair: m3 reinstall", file=sys.stderr)
        return 1
    script = Path(root, *hook.script.split("/"))
    sys.argv = [str(script), *hook.args, *(argv or [])]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


__all__ = [
    "HOOKS",
    "MARKER",
    "argv_for",
    "command_for",
    "hooks_dir",
    "install_launchers",
    "is_launcher_command",
    "launcher_path",
    "run",
]
