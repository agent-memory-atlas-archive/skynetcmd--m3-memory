"""CLI start-up budget (lazy imports design, approved 2026-10-12).

Each `m3` call is a new process when an agent falls back to the CLI, so a
command must not import what it does not use. Measured 2026-10-12 on a Windows box:
`m3 config get` took ~360 ms, ~270 ms of it the tool catalog (with httpx and the
Rust core under it) and the version lookup, none of which it uses; after the
change ~150 ms. This pins it: these modules must not load for these commands.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Heavy modules a simple command never needs (cumulative cost measured 2026-10-12).
HEAVY = ("mcp_tool_catalog", "catalog.dispatch", "m3_sdk", "httpx", "m3_core_rs", "importlib.metadata")


def _imported(*args: str) -> set[str]:
    out = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "m3_memory.cli", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    names = set()
    for line in out.stderr.splitlines():
        if line.startswith("import time:") and "|" in line:
            names.add(line.rsplit("|", 1)[1].strip())
    assert names, f"no -X importtime output; the command failed? rc={out.returncode} {out.stderr[-400:]}"
    return names


@pytest.mark.parametrize(
    "args",
    # Not `config show`: it reports each secret's source, which runs the secrets
    # chain (auth_utils, the keyring and its entry points), so it uses them.
    [("--help",), ("config", "get", "search.row_cap")],
    ids=["help", "config-get"],
)
def test_a_simple_command_imports_nothing_heavy(args):
    loaded = _imported(*args)
    assert not [m for m in HEAVY if m in loaded], f"m3 {' '.join(args)} imported {[m for m in HEAVY if m in loaded]}"


def test_a_domain_command_still_gets_its_tools():
    """The catalog loads when the command line names a domain, and only then."""
    loaded = _imported("memory", "memory_search", "--help")
    assert "mcp_tool_catalog" in loaded


def test_every_tool_domain_is_listed_without_the_catalog():
    """The stubs come from tool_domains.DOMAIN_DESCRIPTIONS, so a domain with
    tools but no description would vanish from `m3 --help`."""
    sys.path.insert(0, str(REPO / "bin"))
    import mcp_tool_catalog
    import tool_domains

    from m3_memory import cli

    used = {tool_domains.domain_of_tool(t.name) for t in mcp_tool_catalog.TOOLS if t.name not in cli._CLI_TOOL_EXCLUDE}
    assert used <= set(tool_domains.DOMAIN_DESCRIPTIONS), used - set(tool_domains.DOMAIN_DESCRIPTIONS)


@pytest.mark.parametrize(
    "argv, word",
    [
        (["memory", "memory_search"], "memory"),
        (["--config", "x.toml", "chat", "status"], "chat"),
        (["--caller", "upgrade", "config", "get"], "config"),
        (["--config=x.toml", "files"], "files"),
        (["--help"], None),
    ],
)
def test_the_command_word_skips_global_options(argv, word):
    from m3_memory import cli

    assert cli._command_word(argv) == word


def test_the_global_options_that_take_a_value_are_the_ones_the_parser_has():
    """_command_word restates which global options take a value. This compares it
    with `m3 --help`, so a new value-taking option cannot make the command word
    (and with it the domain to build) drift silently."""
    import re

    from m3_memory import cli

    out = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", "--help"], cwd=REPO, capture_output=True, text=True, timeout=120
    ).stdout
    options = out.split("options:", 1)[1] if "options:" in out else out
    with_value = set(re.findall(r"^\s{2}(--[a-z][a-z-]*)[ =](?:[A-Z]+|\{[^}]*\})", options, re.MULTILINE))
    assert with_value == set(cli._GLOBAL_VALUE_OPTIONS), with_value


def test_a_domain_whose_catalog_cannot_import_says_so(monkeypatch, capsys):
    """Not argparse's "invalid choice", which names the wrong problem (§3)."""
    import builtins

    from m3_memory import cli

    real = builtins.__import__

    def no_catalog(name, *a, **k):
        if name == "mcp_tool_catalog":
            raise ImportError("planted: catalog broken")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_catalog)
    monkeypatch.setattr(sys, "argv", ["m3", "memory", "memory_search", "--query", "x"])
    with pytest.raises(SystemExit) as done:
        cli.main()
    err = capsys.readouterr().err
    assert done.value.code == 1 and "needs the tool catalog" in err and "planted: catalog broken" in err, err
