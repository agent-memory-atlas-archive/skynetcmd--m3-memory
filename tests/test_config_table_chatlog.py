"""Registry tables [chatlog], [ingest], [observer], [reflector], [slm], [loop],
[autonomy], [governor], [wiki.*]."""

from __future__ import annotations

import os

_C = "memory.config"
_PINNED = {
    "memory.auto_related_link": (_C, "AUTO_RELATED_LINK"),
    "memory.auto_related_link_scope_by_variant": (_C, "AUTO_RELATED_LINK_SCOPE_BY_VARIANT"),
    "ingest.event_rows": (_C, "INGEST_EVENT_ROWS"),
    "ingest.gist_rows": (_C, "INGEST_GIST_ROWS"),
    "ingest.window_chunks": (_C, "INGEST_WINDOW_CHUNKS"),
    "ingest.window_size": (_C, "INGEST_WINDOW_SIZE"),
    "ingest.gist_min_turns": (_C, "INGEST_GIST_MIN_TURNS"),
    "ingest.gist_stride": (_C, "INGEST_GIST_STRIDE"),
    "observer.profile": ("run_observer", "PROFILE_NAME"),
    "reflector.profile": ("run_reflector", "PROFILE_NAME"),
    "governor.feedback_window_minutes": ("memory_maintenance", "_FEEDBACK_WINDOW_MINUTES_DEFAULT"),
}


def test_defaults_are_what_todays_readers_produce():
    from config_defaults_helper import mismatches

    wrong = mismatches(_PINNED)
    assert not wrong, f"registry default != today's reader (registry, today): {wrong}"


def test_statusline_zero_now_reads_as_off():
    """The old reader hid it only for the exact string "off"; "0" and "false" kept
    it on. `m3 config import-env` copies a leftover value with the registry's
    reading (coerce_env_value), so "0" lands as false."""
    from m3_core.config import REGISTRY

    key = REGISTRY.get("chatlog.statusline")
    assert key.coerce_env_value("0") is False and key.coerce_env_value("false") is False


def test_slm_profile_dirs_split_on_the_os_separator():
    """import-env's reading of a leftover M3_SLM_PROFILES_DIR: split on os.pathsep."""
    from m3_core.config import REGISTRY

    raw = os.pathsep.join(["/a", " ", "/b "])
    assert REGISTRY.get("slm.profile_dirs").coerce_env_value(raw) == ["/a", "/b"]


def test_decided_names():
    from m3_core.config import REGISTRY
    from m3_core.config.keys import NOT_SETTINGS, RETIRED

    assert "CHATLOG_MODE" in RETIRED
    assert {"M3_WIKI_DRIFT_PRECISION_FLOOR", "M3_WIKI_DRIFT_RECALL_FLOOR"} <= set(NOT_SETTINGS)
    assert REGISTRY.by_env("M3_CONSOLIDATION_AUTO").name == "autonomy.consolidation_auto"
