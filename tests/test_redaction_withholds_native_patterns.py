"""chatlog_redaction never hands a custom regex's text to status, whatever the
Rust core says. A core older than the m3-redact fix quotes the bad pattern
(``custom_regex compilation error: <pattern>: regex parse error ...``) and does
not report unknown groups; the pattern is often the secret it is meant to catch.
The core is a recorder here, so this runs on every lane, with or without it.
"""

from __future__ import annotations

import importlib

import pytest


class _OldCore:
    """The 3.10.7 shape: the pattern quoted, no unknown-group errors."""

    def __init__(self):
        self.errors: list[str] = []

    def scrub(self, content, config):
        self.errors = [
            f"custom_regex compilation error: {p}: regex parse error:\n    {p}\n    ^\nerror: unclosed group"
            for p in config.get("custom_regex", [])
            if p.startswith(("[", "("))
        ]
        return content, 0, []

    def redaction_compile_errors(self):
        return list(self.errors)


@pytest.fixture
def clr(monkeypatch):
    mod = importlib.import_module("chatlog_redaction")
    monkeypatch.setattr(mod, "m3_core_rs", _OldCore())
    monkeypatch.setattr(mod, "_LAST_NATIVE_CONFIG", None)
    return mod


def _cfg(patterns, custom):
    return {"enabled": True, "patterns": patterns, "custom_regex": custom, "redact_pii": False}


def test_old_core_errors_are_reworded_without_the_pattern(clr):
    c = _cfg(["custom_regex", "api_kyes"], ["[TOPSECRET", r"GOOD_\d+", "(also(bad"])
    clr.scrub("x", c)
    errors = clr.get_compile_errors()
    assert not any("TOPSECRET" in e or "also(bad" in e for e in errors), errors
    assert errors == [
        clr._unknown_group_error(1, "api_kyes"),
        clr._custom_regex_error(0, 10, "invalid regex syntax"),
        clr._custom_regex_error(2, 9, "invalid regex syntax"),
    ]


def test_an_error_naming_no_known_pattern_is_withheld_entirely(clr):
    clr.scrub("x", _cfg(["custom_regex"], ["[a"]))
    clr.m3_core_rs.errors = ["custom_regex compilation error: [zzz: regex parse error"]
    (only,) = clr.get_compile_errors()
    assert "zzz" not in only and "text withheld" in only


def test_safe_core_errors_pass_through_unchanged(clr):
    c = _cfg(["custom_regex", "nope"], ["[x"])
    clr.scrub("x", c)
    safe = [clr._unknown_group_error(1, "nope"), clr._custom_regex_error(0, 2, "invalid regex syntax")]
    clr.m3_core_rs.errors = list(safe)
    assert clr.get_compile_errors() == safe
