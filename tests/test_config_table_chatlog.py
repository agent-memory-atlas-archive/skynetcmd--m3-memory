"""Registry tables [chatlog], [ingest], [observer], [reflector], [slm], [loop],
[autonomy], [governor], [wiki.*] (plan 2C.1)."""

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


def test_statusline_zero_now_reads_as_off(tmp_path, caplog):
    """Today only the exact string "off" hid it; "0" and "false" kept it on."""
    import logging

    from m3_core.config import REGISTRY, Location
    from m3_core.config import resolve as r
    from m3_core.config.settings import build

    r._LEGACY_NOTED.discard(("M3_CHATLOG_STATUSLINE", "0"))
    with caplog.at_level(logging.WARNING):
        s = build(
            Location(file=tmp_path / "m3_config.toml", root=tmp_path, tier="t"), environ={"M3_CHATLOG_STATUSLINE": "0"}
        )
    assert s[REGISTRY.get("chatlog.statusline")] is False and "now reads as False" in caplog.text


def test_slm_profile_dirs_split_on_the_os_separator(tmp_path):
    from m3_core.config import REGISTRY, Location
    from m3_core.config.settings import build

    raw = os.pathsep.join(["/a", " ", "/b "])
    s = build(Location(file=tmp_path / "m3_config.toml", root=tmp_path, tier="t"), environ={"M3_SLM_PROFILES_DIR": raw})
    assert s[REGISTRY.get("slm.profile_dirs")] == ["/a", "/b"]


def test_decided_names():
    from m3_core.config import REGISTRY
    from m3_core.config.keys import NOT_SETTINGS, RETIRED

    assert "CHATLOG_MODE" in RETIRED
    assert {"M3_WIKI_DRIFT_PRECISION_FLOOR", "M3_WIKI_DRIFT_RECALL_FLOOR"} <= set(NOT_SETTINGS)
    assert REGISTRY.by_env("M3_CONSOLIDATION_AUTO").name == "autonomy.consolidation_auto"
