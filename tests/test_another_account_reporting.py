"""On a shared machine, another account's server is never reported as ours.

Found in a trial install beside an existing m3 user: the new account's
dashboard could not bind, yet the start check printed "Started ... (serving)"
because the other account's dashboard answered on the same port, and setup's
summary called the other account's embedder this install's "local server ...
with the speed-up module".
"""

from __future__ import annotations

import os
import sys
import types

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (os.path.join(_ROOT, "bin"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import install_schedules as isch  # noqa: E402

from m3_memory import _ports, embedder_admin, installer, rust_core_install  # noqa: E402


def test_start_check_does_not_count_another_accounts_answer(monkeypatch):
    monkeypatch.setattr(_ports, "held_by_another_owner", lambda *a, **k: True)
    detail = isch._confirm_service_live("m3-dashboard.service", deadline_s=0.1)
    assert detail is not True
    assert "another account" in detail


def test_start_check_still_polls_when_the_port_is_ours(monkeypatch):
    monkeypatch.setattr(_ports, "held_by_another_owner", lambda *a, **k: False)
    detail = isch._confirm_service_live("m3-dashboard.service", deadline_s=0.1)
    assert detail is True or "another account" not in detail


def _shared_mode(monkeypatch):
    monkeypatch.setattr(embedder_admin, "shared_mode_enabled", lambda: True)
    fake = types.ModuleType("m3_core_rs")
    fake.EmbeddedEmbedder = object
    fake.__version__ = "3.10.1"
    monkeypatch.setitem(sys.modules, "m3_core_rs", fake)


def test_tier_summary_names_another_accounts_server(monkeypatch):
    _shared_mode(monkeypatch)
    monkeypatch.setattr(embedder_admin, "_port_held_by_someone_else", lambda *a, **k: True)
    tier = rust_core_install.active_embedder_tier()
    assert tier["summary"] == "another account's server on port 8082"
    assert tier["others_server"] is True
    assert "speed-up module" not in tier["summary"]


def test_headline_names_another_accounts_server(monkeypatch):
    _shared_mode(monkeypatch)
    monkeypatch.setattr(embedder_admin, "_port_held_by_someone_else", lambda *a, **k: True)
    monkeypatch.setattr(rust_core_install, "active_embedder_tier", lambda: {"native": True})
    assert installer.status_summary()["embedder"] == "another account's server (port 8082)"


def test_start_check_reason_is_recognised_by_its_caller(monkeypatch):
    """The caller drops the "check its log" advice for this reason; the two
    must share the one phrase."""
    monkeypatch.setattr(_ports, "held_by_another_owner", lambda *a, **k: True)
    detail = isch._confirm_service_live("m3-dashboard.service", deadline_s=0.1)
    assert isch._HELD_BY_OTHER in detail
