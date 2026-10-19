"""The agent hosts whose chat capture CI runs end to end, and the files each one
depends on. The host-capture workflow asks this module which hosts a change
touches (``python tests/host_capture.py hosts --changed -`` with the changed
paths on stdin); tests/test_host_capture.py runs one host per job.

A path matching COMMON touches every host. Patterns use fnmatch, where ``*``
also matches ``/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass(frozen=True, slots=True)
class Host:
    # The launcher m3 setup points the host at (m3_memory.hook_launchers.HOOKS);
    # empty for a host whose capture is not a hook (Aider's watcher).
    hook: str
    # How the host starts the hook: "line" (a command in its config) or "js"
    # (m3_memory.host_plugins code the host runs).
    runs: str
    files: tuple[str, ...]


HOSTS: dict[str, Host] = {
    "claude-code": Host(
        "claude-capture", "line", ("bin/hooks/chatlog/claude_code_precompact.*", "bin/generate_configs.py")
    ),
    "codex": Host("codex-capture", "line", ("bin/hooks/chatlog/codex_stop.*",)),
    "gemini-cli": Host("gemini-capture", "line", ("bin/hooks/chatlog/gemini_cli_onexit.*",)),
    "antigravity": Host(
        "antigravity-capture", "line", ("bin/hooks/chatlog/antigravity_stop.*", "m3_memory/antigravity.py")
    ),
    "opencode": Host(
        "opencode-capture", "js", ("bin/hooks/chatlog/opencode_session_end.*", "m3_memory/host_plugins.py")
    ),
    "openclaw": Host(
        "openclaw-capture", "js", ("bin/hooks/chatlog/openclaw_session_end.*", "m3_memory/host_plugins.py")
    ),
    "aider": Host("", "watcher", ("bin/hooks/chatlog/aider_chat_watcher.*",)),
}

COMMON = (
    "bin/chatlog_*.py",
    "bin/m3_core/*",
    "bin/memory/*",
    "m3_memory/hook_launchers.py",
    "m3_memory/installer.py",
    "m3_memory/_bin.py",
    "m3_memory/_roots.py",
    "pyproject.toml",
    "tests/host_capture.py",
    "tests/test_host_capture.py",
    ".github/workflows/host-capture.yml",
)


def hosts_for(paths: list[str]) -> list[str]:
    """The hosts a change to ``paths`` touches, in HOSTS order."""
    paths = [p.strip().replace("\\", "/") for p in paths if p.strip()]
    if any(fnmatch(p, pat) for p in paths for pat in COMMON):
        return list(HOSTS)
    out = []
    for name, host in HOSTS.items():
        own = (*host.files, f"tests/fixtures/hosts/{name}/*")
        if any(fnmatch(p, pat) for p in paths for pat in own):
            out.append(name)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Which agent hosts' capture jobs a change needs.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hosts")
    g = h.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="every host")
    g.add_argument("--changed", metavar="FILE", help="file of changed paths, one per line ('-' for stdin)")
    args = ap.parse_args(argv)
    if args.all:
        print(json.dumps(list(HOSTS)))
        return 0
    if args.changed == "-":
        paths = sys.stdin.read().splitlines()
    else:
        with open(args.changed, encoding="utf-8") as fh:
            paths = fh.read().splitlines()
    print(json.dumps(hosts_for(paths)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
