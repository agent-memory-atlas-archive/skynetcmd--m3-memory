"""The backend selector reads registry key ``db.backend``,
an ordinary key since 2026-10-12: a flag, then
``[db] backend`` in m3_config.toml, then ``M3_DB_BACKEND`` as a legacy name BELOW
the file (warned once), then sqlite. Before the registry, the selector read env
only, so a backend chosen in the file was silently ignored: a PostgreSQL install
would have run on SQLite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


@pytest.fixture
def postgres_in_file():
    """M3_DB_BACKEND / DB_BACKEND start unset: the conftest sandbox clears them."""
    from m3_core.config import settings as st

    from m3_memory import _roots

    root = Path(_roots.config_root())
    root.mkdir(parents=True, exist_ok=True)
    (root / "m3_config.toml").write_text('schema_version = 1\n[db]\nbackend = "postgres"\n', encoding="utf-8")
    st._reset_for_tests()
    yield
    st._reset_for_tests()


def test_the_file_selects_the_backend(postgres_in_file):
    from m3_core.paths import resolve_backend_name

    assert resolve_backend_name() == "postgres"


def test_the_file_beats_env(postgres_in_file, monkeypatch):
    from m3_core.config import settings as st
    from m3_core.paths import resolve_backend_name

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    st._reset_for_tests()
    assert resolve_backend_name() == "postgres"


def test_the_legacy_env_name_answers_below_the_file_and_warns_once(monkeypatch, caplog):
    """With no [db] backend in the file, M3_DB_BACKEND still selects through N+3,
    and says so once per process: a daemon on PostgreSQL because of a variable
    nobody sees must not be silent (§3)."""
    import logging

    from m3_core import paths
    from m3_core.config import settings as st

    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setattr(paths, "_BACKEND_ENV_WARNED", set())
    st._reset_for_tests()
    with caplog.at_level(logging.WARNING):
        assert paths.resolve_backend_name() == "postgres"
        assert paths.resolve_backend_name() == "postgres"
    said = [r.getMessage() for r in caplog.records if "M3_DB_BACKEND" in r.getMessage()]
    assert len(said) == 1 and "m3 config set db.backend postgres" in said[0], said


def test_a_flag_beats_env(postgres_in_file, monkeypatch):
    from m3_core.config import settings as st
    from m3_core.paths import resolve_backend_name

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    st.set_from_cli({"db.backend": "postgres"})
    assert resolve_backend_name() == "postgres"
