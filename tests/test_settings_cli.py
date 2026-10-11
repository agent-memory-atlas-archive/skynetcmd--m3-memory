"""settings.set_from_cli: a command-line flag as a setting for this process
(origin "cli"), replacing ``os.environ[NAME] = flag`` (R-explicit: an explicit
flag beats env; env is only a default)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def test_a_flag_beats_env_and_never_writes_env(monkeypatch):
    from m3_core.config import settings as st

    monkeypatch.setenv("M3_EMBED_URL", "http://from-env:1")
    st._reset_for_tests()
    assert st.setting("embed.url") == "http://from-env:1"
    st.set_from_cli({"embed.url": "http://from-flag:2"})
    assert st.setting("embed.url") == "http://from-flag:2"
    assert st.origin_of("embed.url") == "cli"
    assert os.environ["M3_EMBED_URL"] == "http://from-env:1", "a flag must not write env"


def test_a_flag_not_given_changes_nothing():
    from m3_core.config import settings as st

    st._reset_for_tests()
    before = st.origin_of("embed.url")
    st.set_from_cli({"embed.url": None})
    assert st.origin_of("embed.url") == before


def test_flag_text_is_parsed_and_validated():
    from m3_core.config import ConfigValueError
    from m3_core.config import settings as st

    st._reset_for_tests()
    st.set_from_cli({"dashboard.port": "9123"})
    assert st.setting("dashboard.port") == 9123
    with pytest.raises((ConfigValueError, ValueError)):
        st.set_from_cli({"dashboard.port": "70000"})
    with pytest.raises(KeyError, match="not a registry key"):
        st.set_from_cli({"no.such.key": "x"})


def test_a_flag_survives_a_config_reload(tmp_path, monkeypatch):
    """The snapshot rebuilds when m3_config.toml changes; a flag is re-applied."""
    from m3_core.config import settings as st

    st._reset_for_tests()
    st.set_from_cli({"embed.url": "http://from-flag:2"})
    cache = st._CACHE
    cache._settings = cache._with_cli(st.build())  # what a reload installs
    assert st.setting("embed.url") == "http://from-flag:2"
