"""The m3_settings fixture: configuration for a
test without touching os.environ, and hermetic against the developer's env."""

from __future__ import annotations

import pytest


def test_installed_values_are_what_current_returns(m3_settings):
    from m3_core.config import REGISTRY
    from m3_core.config import settings as st

    m3_settings({"search.row_cap": 10, "db.backend": "postgres"})
    snap = st.current()
    assert snap[REGISTRY.get("search.row_cap")] == 10
    assert snap.origin(REGISTRY.get("db.backend")) == "override"


def test_the_developers_env_does_not_leak_in(m3_settings, monkeypatch):
    from m3_core.config import REGISTRY

    monkeypatch.setenv("M3_SEARCH_ROW_CAP", "123")
    snap = m3_settings()
    assert snap[REGISTRY.get("search.row_cap")] == REGISTRY.get("search.row_cap").default


def test_values_are_validated(m3_settings):
    from m3_core.config import ConfigValueError

    with pytest.raises(ConfigValueError):
        m3_settings({"db.backend": "postgre"})


def test_an_unknown_name_is_refused(m3_settings):
    with pytest.raises(AssertionError, match="not registry keys"):
        m3_settings({"search.rowcap": 10})


def test_it_does_not_survive_the_test(m3_settings):
    """Run after test_installed_values...: the sandbox reset rebuilt from defaults."""
    from m3_core.config import REGISTRY
    from m3_core.config import settings as st

    assert st.current().origin(REGISTRY.get("search.row_cap")) != "override"


def test_installed_values_survive_a_pin_left_by_an_earlier_test(m3_settings, tmp_path):
    """Order-dependent failure, found when new tests shifted the run order: a
    test that pinned a config file left the cache remembering the pin, and the
    next installed snapshot was dropped as "the pin changed"."""
    from m3_core.config import REGISTRY
    from m3_core.config import settings as st

    from m3_memory import _roots

    f = tmp_path / "m3_config.toml"
    f.write_text("schema_version = 1\n", encoding="utf-8")
    _roots.pin_config_file(f)
    st.current()  # the cache now remembers the pin
    _roots.pin_config_file(None)  # what the sandbox does between tests
    m3_settings({"search.row_cap": 10})
    assert st.current()[REGISTRY.get("search.row_cap")] == 10
