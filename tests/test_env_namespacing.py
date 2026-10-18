"""Settings come from m3_config.toml: neither the M3_* name nor the bare legacy
name of a setting is read.

The M3_* namespacing migration once made both spellings work (getenv_compat);
the deprecation window is over (decision 2026-10-17), so both are ignored and
the file's value, else the registry default, applies. memory.config exposes
these as registry accessors (read per access), so no reload is needed: the
snapshot is rebuilt after each change.
"""

from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


def _reload_config(monkeypatch, env: dict, toml: str = ""):
    from m3_core.config import settings as st

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if toml:
        cfg = os.path.join(os.environ["M3_CONFIG_ROOT"], "m3_config.toml")
        with open(cfg, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n" + toml)
    st._reset_for_tests()
    return importlib.import_module("memory.config")


def test_neither_spelling_is_read(monkeypatch):
    c = _reload_config(
        monkeypatch,
        {"M3_DEDUP_THRESHOLD": "0.5", "DEDUP_THRESHOLD": "0.7", "M3_EMBED_DIM": "384", "EMBED_DIM": "256"},
    )
    assert c.DEDUP_THRESHOLD == 0.92  # documented default
    assert c.EMBED_DIM == 1024


def test_the_file_value_is_used(monkeypatch):
    c = _reload_config(
        monkeypatch,
        {"CONTRADICTION_TITLE_GATE": "strict", "M3_DEDUP_THRESHOLD": "0.5"},
        "[memory.dedup]\nthreshold = 0.8\n",
    )
    from m3_core.config import REGISTRY

    assert c.DEDUP_THRESHOLD == 0.8
    default_gate = REGISTRY.get("memory.contradiction.title_gate").default
    assert c.CONTRADICTION_TITLE_GATE == default_gate.lower(), "the bare legacy name is not read"


def test_default_applies_when_nothing_is_set(monkeypatch):
    c = _reload_config(monkeypatch, {})
    assert c.DEDUP_THRESHOLD == 0.92  # documented default
    assert c.EMBED_DIM == 1024


def test_a_set_old_name_is_reported_as_not_read(monkeypatch):
    """The resolver records an old name that is set, so doctor can name it."""
    from m3_core.config import settings as st

    monkeypatch.setenv("DEDUP_THRESHOLD", "0.7")
    st._reset_for_tests()
    assert "DEDUP_THRESHOLD" in st.current().shadowed_env()
