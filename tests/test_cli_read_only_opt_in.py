"""An opt-in tool that only reads runs from the CLI without --yes; a destructive one still asks.

`default_allowed=False` means opt-in on MCP. The CLI read it as "destructive",
so `m3 memory memory_search_routed` demanded --yes and said the search
"mutates/deletes data".
"""

from __future__ import annotations

import sys

_READS = ("memory_search_routed", "memory_search_multi_db", "memory_export", "gdpr_export")


def _spec(name):
    import mcp_tool_catalog as cat

    return next(t for t in cat.TOOLS if t.name == name)


def test_read_only_opt_in_tools_are_not_destructive():
    for name in _READS:
        s = _spec(name)
        assert not s.default_allowed and not s.is_destructive, name
    assert _spec("memory_delete").is_destructive
    assert _spec("gdpr_forget").is_destructive


def _cli(monkeypatch, capsys, *argv):
    from m3_memory import cli

    monkeypatch.setattr(sys, "argv", ["m3", *argv])
    try:
        rc = cli.main()
    except SystemExit as e:
        rc = e.code
    return rc, capsys.readouterr().err


def test_the_cli_runs_a_read_only_opt_in_search_without_yes(monkeypatch, capsys):
    _rc, err = _cli(monkeypatch, capsys, "memory", "memory_search_routed", "--query", "nothing here")
    assert "mutates/deletes data" not in err, err


def test_the_cli_still_asks_before_a_delete(monkeypatch, capsys):
    rc, err = _cli(monkeypatch, capsys, "memory", "memory_delete", "--id", "00000000-0000-0000-0000-000000000000")
    assert rc and "mutates/deletes data" in err, (rc, err)
