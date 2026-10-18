"""doctor's decoupled-roots section.

The roots come from m3_config.toml [paths]; only M3_CONFIG_ROOT is read from the
environment (M3_MEMORY_ROOT / M3_ENGINE_ROOT are not). The section reports where
each root resolves. These tests keep the sandbox's config root (its [paths] pins
every root under tmp), so the section never reads the developer's real ~/.m3.
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
    # M3_CONFIG_ROOT stays: it is the sandbox's, and its file pins every root.
    for v in ("M3_MEMORY_ROOT", "M3_ENGINE_ROOT"):
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


def test_an_engine_root_env_pin_is_not_read(monkeypatch):
    """m3 reads one variable, M3_CONFIG_ROOT (user decision 2026-10-17): an
    M3_ENGINE_ROOT pin changes nothing; the engine root is the file's. (doctor's
    last section names the variable, to delete.) The section used to say "pin
    BOTH M3_ENGINE_ROOT and M3_CONFIG_ROOT", the opposite."""
    from m3_sdk import get_m3_engine_root

    _clear_roots(monkeypatch)
    monkeypatch.setenv("M3_ENGINE_ROOT", os.path.join(os.sep, "tmp", "eng"))
    out = _roots_output()
    assert str(get_m3_engine_root()) in out
    assert os.path.join(os.sep, "tmp", "eng") not in out
    assert "ASYMMETRIC" not in out and "Pin M3_ENGINE_ROOT + M3_CONFIG_ROOT" not in out


def test_m3_config_root_alone_is_not_flagged(monkeypatch):
    """The one variable m3 reads: setting it is not a finding here."""
    _clear_roots(monkeypatch)
    out = _roots_output()  # the sandbox sets M3_CONFIG_ROOT to its own tmp config
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
