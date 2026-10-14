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
