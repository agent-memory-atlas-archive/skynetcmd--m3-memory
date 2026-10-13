"""Every place an environment name is set, with its value: the one sweep.

``installer._find_env_var_locations`` (doctor's "set in ...", the PG_URL refusal)
and ``m3 config import-env`` (plan 2F.1) both read it, so a place one of them
knows about the other cannot miss. Sources:

  - this process's environment;
  - each MCP client's ``mcpServers.<server>.env`` block, including Claude Code's
    ``~/.claude.json``, where ``claude mcp add --env`` writes the roots;
  - the ``.env`` m3 reads, and one in the working directory;
  - shell startup files in ``$HOME``;
  - the Windows User and Machine persistent environment;
  - the legacy ``<config_root>/.llm_config.json``.

Read-only and never raises: a place it could not read is named in ``unchecked``,
because an unread file is not a file without the name (DESIGN_PHILOSOPHIES §3).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

RC_FILES = (".zshenv", ".zshrc", ".zprofile", ".bashrc", ".bash_profile", ".bash_login", ".profile")


@dataclass(frozen=True, slots=True)
class EnvFound:
    name: str
    value: str
    where: str  # a human-readable location; for a file, its path
    kind: str  # "process" | "mcp" | "dotenv" | "rc" | "registry" | "llmfile"
    server: str | None = None  # kind "mcp": the server entry's name
    m3_server: bool = False  # kind "mcp": the entry runs m3 (not another tool)

    @property
    def place(self) -> str:
        """``where``, plus the server for an MCP entry: for a person reading."""
        return f"{self.where} (server {self.server!r})" if self.server else self.where

    @property
    def persistent(self) -> bool:
        """Set in a place that outlives this process, and m3's own: the places
        import-env may take a secret from (user decision, m3 a8083888). Another
        tool's MCP entry is never one (m3 c7082b20: report only)."""
        return self.kind != "process" and (self.kind != "mcp" or self.m3_server)


# The server names m3 registers itself under: `claude mcp add m3_memory` and the
# "memory" entry written for the other clients (generate_configs).
M3_SERVER_NAMES = frozenset({"m3_memory", "memory"})


def is_m3_server(name: str, entry: dict) -> bool:
    """Whether an MCP server entry runs m3: one of m3's names, or a command line
    that launches m3 by the process classifier's exact entry-point rule
    (``m3_core.process.runs_m3``; a substring test once misread
    ``test_memory_bridge.py`` as m3). A user's own server is never one."""
    if name in M3_SERVER_NAMES:
        return True
    from m3_memory import _bin

    _bin.ensure_on_path()
    from m3_core.process import runs_m3

    return bool(runs_m3([str(entry.get("command") or ""), *(str(a) for a in (entry.get("args") or []))]))


def shell_assignment(text: str, name: str) -> str | None:
    """The value of the LAST assignment of ``name`` at a line start in shell
    startup text (``NAME=``, ``export NAME=``, csh ``set NAME=``), or None. A
    commented-out line does not count, and ``M3_CDW_PG_URL=`` is not ``PG_URL``.
    The one owner of this reading."""
    last = None
    for m in re.finditer(rf"(?m)^\s*(?:export\s+|set\s+)?{re.escape(name)}\s*=(.*)$", text):
        rhs = m.group(1).strip()
        if rhs[:1] in ('"', "'") and rhs.count(rhs[0]) >= 2:
            rhs = rhs[1 : rhs.index(rhs[0], 1)]
        else:
            rhs = rhs.split("#", 1)[0].strip()
        last = rhs
    return last


def _home() -> Path:
    """The home directory the sweep's own home files (rc files, ~/.claude.json)
    are read under. The test sandbox points it at a temp directory, and rebases
    the client config list the same way, so a test never reads the developer's
    real files."""
    return Path.home()


def claude_user_config() -> Path:
    """Claude Code's user config, where `claude mcp add` writes server env blocks."""
    return _home() / ".claude.json"


def mcp_config_files() -> list[Path]:
    """Every file an MCP client reads its server env blocks from."""
    from m3_memory import installer as _I  # lazy: installer imports install/*

    files = {p for sources in _I._client_config_sources().values() for p in sources}
    files.add(claude_user_config())
    return sorted(files)


def _server_entries(data: dict) -> Iterable[tuple[str, dict]]:
    yield from (data.get("mcpServers") or {}).items()
    for project in (data.get("projects") or {}).values():  # ~/.claude.json project scope
        yield from ((project or {}).get("mcpServers") or {}).items()


def scan(
    names: Iterable[str], *, include_process_env: bool = True, unchecked: list[str] | None = None
) -> list[EnvFound]:
    """Each place any of ``names`` is set, and to what. Unsorted, one entry per
    (place, name); a value of "" means set but empty."""
    wanted = list(dict.fromkeys(names))
    skipped = unchecked if unchecked is not None else []
    out: list[EnvFound] = []

    if include_process_env:
        out += [
            # env-owner: the doctor / import-env scan: reading env is its job
            EnvFound(n, os.environ[n], f"environment (os.environ['{n}'])", "process")
            for n in wanted
            if n in os.environ  # env-owner: the doctor / import-env scan: reading env is its job
        ]

    try:
        files = mcp_config_files()
    except Exception as e:  # noqa: BLE001 -- informational sweep, never fatal
        skipped.append(f"MCP client configs ({type(e).__name__}: {e})")
        files = []
    for path in files:
        if not os.path.isfile(path):  # os.path: Path.is_file raises under an untraversable parent (3.12/3.13)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as e:
            skipped.append(f"{path} ({type(e).__name__}: {e})")
            continue
        for server, entry in _server_entries(data if isinstance(data, dict) else {}):
            entry = entry if isinstance(entry, dict) else {}
            env_block = entry.get("env") or {}
            ours = is_m3_server(server, entry)
            out += [EnvFound(n, str(env_block[n]), str(path), "mcp", server, ours) for n in wanted if n in env_block]

    out += _dotenv_hits(wanted, skipped)

    home = _home()
    for rc in RC_FILES:
        f = home / rc
        try:
            if not os.path.isfile(f):
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            skipped.append(f"{f} ({type(e).__name__}: {e})")
            continue
        for n in wanted:
            value = shell_assignment(text, n)
            if value is not None:
                out.append(EnvFound(n, value, str(f), "rc"))

    from m3_memory import installer as _I

    for hit in _I._scan_registry_env_deprecations(dict.fromkeys(wanted, ""), skipped):
        out.append(EnvFound(hit["old"], hit.get("value", ""), hit["label"], "registry"))

    try:
        from m3_memory import _bin

        _bin.ensure_on_path()
        from m3_core.config.env import llm_file

        llm_path, llm_values = llm_file()
        out += [EnvFound(n, llm_values[n], str(llm_path), "llmfile") for n in wanted if n in llm_values]
    except Exception as e:  # noqa: BLE001 -- informational sweep, never fatal
        skipped.append(f".llm_config.json ({type(e).__name__}: {e})")
    return out


def _dotenv_hits(wanted: list[str], skipped: list[str]) -> list[EnvFound]:
    """The .env m3 reads (config root, then memory root) and one in the cwd."""
    paths: list[Path] = []
    try:
        from m3_memory import _bin

        _bin.ensure_on_path()
        from m3_core.paths import dotenv_path

        found = dotenv_path()
        if found:
            paths.append(Path(found))
    except Exception as e:  # noqa: BLE001 -- informational sweep, never fatal
        skipped.append(f"m3's .env ({type(e).__name__}: {e})")
    cwd = Path.cwd() / ".env"
    if cwd not in paths:
        paths.append(cwd)
    out: list[EnvFound] = []
    for path in paths:
        try:
            if not os.path.isfile(path):
                continue
            from dotenv import dotenv_values

            values = dotenv_values(path)
        except Exception as e:  # noqa: BLE001 -- an unparseable .env is named, not fatal
            skipped.append(f"{path} ({type(e).__name__}: {e})")
            continue
        # A bare NAME line sets nothing (as m3_core.config.env.dotenv reads it).
        out += [EnvFound(n, v, str(path), "dotenv") for n in wanted if (v := values.get(n)) is not None]
    return out
