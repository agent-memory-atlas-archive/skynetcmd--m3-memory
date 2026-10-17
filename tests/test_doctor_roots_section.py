"""doctor's decoupled-roots section + split-brain detection.

The three-root model (M3_MEMORY_ROOT / M3_ENGINE_ROOT / M3_CONFIG_ROOT) lets the
engine (DBs) and config relocate independently of the repo. The documented
hazard (CLAUDE.md "Split-brain hazard") is asymmetric/partial pinning where the
config and engine roots diverge, or are pinned on only one of the MCP-server /
hook surfaces. doctor surfaces both. These tests pin that behavior.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from m3_memory import installer  # noqa: E402


def _roots_output():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        installer._roots_section()
    return buf.getvalue()


def _clear_roots(monkeypatch):
    for v in ("M3_MEMORY_ROOT", "M3_ENGINE_ROOT", "M3_CONFIG_ROOT"):
        monkeypatch.delenv(v, raising=False)


def test_reports_all_three_roots(monkeypatch):
    _clear_roots(monkeypatch)
    out = _roots_output()
    assert "memory root" in out
    assert "config root" in out
    assert "engine root" in out


def test_no_warning_when_nothing_pinned(monkeypatch):
    _clear_roots(monkeypatch)
    out = _roots_output()
    # No env pinned -> no asymmetry warning and no both-surfaces reminder.
    assert "ASYMMETRIC" not in out
    assert "Decoupled-roots reminder" not in out


def test_an_engine_root_pin_is_reported_as_deprecated(monkeypatch):
    """m3 reads one variable, M3_CONFIG_ROOT (user decision 2026-10-17): an
    M3_ENGINE_ROOT pin is deprecated in favour of paths.engine_root. The section
    used to say "pin BOTH M3_ENGINE_ROOT and M3_CONFIG_ROOT", the opposite."""
    _clear_roots(monkeypatch)
    monkeypatch.setenv("M3_ENGINE_ROOT", "/tmp/eng")
    out = _roots_output()
    assert "M3_ENGINE_ROOT set in this process: deprecated" in out
    assert "paths.engine_root" in out
    assert "ASYMMETRIC" not in out and "Pin M3_ENGINE_ROOT + M3_CONFIG_ROOT" not in out


def test_m3_config_root_alone_is_not_flagged(monkeypatch):
    """The one variable m3 reads: setting it is not a finding here."""
    _clear_roots(monkeypatch)
    monkeypatch.setenv("M3_CONFIG_ROOT", "/tmp/cfg")
    out = _roots_output()
    assert "deprecated" not in out and "ASYMMETRIC" not in out


def test_section_never_raises_without_sdk(monkeypatch):
    """Best-effort: a stripped env where m3_sdk isn't importable must not crash
    doctor — the section just no-ops.

    Simulate the failure by making the IMPORT raise, rather than poking
    sys.modules['m3_sdk']=None: the latter leaks a broken entry that a sibling
    test's `importlib.reload(m3_sdk)` then trips over ("module not in
    sys.modules"). Scoping the failure to __import__ keeps sys.modules intact.
    """
    _clear_roots(monkeypatch)
    import builtins

    real_import = builtins.__import__

    def _no_sdk(name, *a, **k):
        if name == "m3_sdk":
            raise ImportError("simulated: m3_sdk unavailable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_sdk)
    # Should not raise.
    out = _roots_output()
    # And it produced nothing (section no-ops on SDK-unavailable).
    assert "decoupled roots" not in out
