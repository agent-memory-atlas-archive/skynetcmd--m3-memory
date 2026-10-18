"""Autonomy toggles: m3_config.toml > .cognitive_loop_config.json > the registry's
legacy env name > default (<dynamic> design Q2, 2026-10-13). The env name is read
by the registry, not by autonomy.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def autonomy():
    from m3_core import autonomy as a

    return a


def _write_file(a, **flags):
    path = Path(a.autonomy_config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(flags), encoding="utf-8")


def test_the_default_is_off(autonomy, m3_settings):
    m3_settings()
    assert autonomy.autonomy_flag("consolidation_auto") is False


def test_the_file_beats_the_default(autonomy, m3_settings):
    m3_settings()
    _write_file(autonomy, consolidation_auto=True)
    assert autonomy.autonomy_flag("consolidation_auto") is True


def test_m3_config_beats_the_file(autonomy, m3_settings):
    m3_settings({"autonomy.consolidation_auto": False})
    _write_file(autonomy, consolidation_auto=True)
    assert autonomy.autonomy_flag("consolidation_auto") is False


def test_the_legacy_env_name_is_not_read(autonomy, monkeypatch):
    """M3_CHATLOG_PRUNE_AUTO is a former name: setting it changes nothing."""
    from m3_core.config import settings as st

    monkeypatch.setenv("M3_CHATLOG_PRUNE_AUTO", "1")
    st._reset_for_tests()
    try:
        assert autonomy.autonomy_flag("chatlog_prune_auto") is False
    finally:
        st._reset_for_tests()


def test_the_seed_records_the_registry_values(autonomy, m3_settings):
    m3_settings({"autonomy.chatlog_prune_auto": True})
    path = Path(autonomy.ensure_autonomy_config())
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["chatlog_prune_auto"] is True and data["consolidation_auto"] is False
