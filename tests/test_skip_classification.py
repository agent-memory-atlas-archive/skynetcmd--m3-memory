"""Every skip names its dimension, and M3_STRICT_SKIPS=1 enforces it.

The classifier and the strict hook live in conftest; a classifier that misreads
a real skip either fails a strict run for nothing (§3: a false alarm is a
violation) or passes an unexplained one.
"""

from __future__ import annotations

import types

import pytest

import conftest
from conftest import SKIP_DIMENSIONS, _skip_dimension, skip_reason


def _importorskip_reason() -> str:
    # The reason an importorskip actually reports at runtime, produced live
    # with the reason= every call site passes (test_skips_name_a_dimension).
    try:
        pytest.importorskip("m3_no_such_module_for_skip_classification", reason=skip_reason("dep", "no such module"))
    except pytest.skip.Exception as e:
        return str(e.msg)
    raise AssertionError("importorskip did not skip on a missing module")


def test_skip_reason_round_trips_every_dimension():
    for dim in SKIP_DIMENSIONS:
        assert _skip_dimension(skip_reason(dim, "x")) == dim


def test_importorskip_reason_survives_to_the_report():
    assert _skip_dimension(_importorskip_reason()) == "dep"


def test_bare_text_is_unclassified():
    # pytest's default importorskip text included: test_skips_name_a_dimension
    # rejects it statically, and the strict hook must agree at runtime.
    assert _skip_dimension("no reachable PostgreSQL") is None
    assert _skip_dimension("could not import 'x': No module named 'x'") is None
    assert _skip_dimension("[nope] unknown dimension") is None


def _finish(monkeypatch, skips, strict):
    monkeypatch.setattr(conftest, "_SKIPS", skips)
    if strict:
        monkeypatch.setenv("M3_STRICT_SKIPS", "1")
    else:
        monkeypatch.delenv("M3_STRICT_SKIPS", raising=False)
    session = types.SimpleNamespace(exitstatus=0)
    conftest.pytest_sessionfinish(session, 0)
    return session.exitstatus


def test_strict_fails_an_unclassified_skip(monkeypatch):
    assert _finish(monkeypatch, [("t::a", "bare reason")], strict=True) == 1


def test_strict_passes_classified_skips(monkeypatch):
    skips = [("t::a", skip_reason("os", "posix only")), ("t::b", _importorskip_reason())]
    assert _finish(monkeypatch, skips, strict=True) == 0


def test_not_strict_never_changes_the_exit(monkeypatch):
    assert _finish(monkeypatch, [("t::a", "bare reason")], strict=False) == 0
