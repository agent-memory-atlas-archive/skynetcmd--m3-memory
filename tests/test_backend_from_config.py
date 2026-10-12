"""The backend selector reads registry key ``db.backend`` (CONFIG_DESIGN §2 item 6):
a flag, then ``M3_DB_BACKEND`` re-read LIVE (U9, the one env_override key), then
``[db] backend`` in m3_config.toml, then sqlite. Before this, the selector read
env only, so a backend chosen in the file was silently ignored: a PostgreSQL
install would have run on SQLite."""

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


def test_env_overrides_the_file(postgres_in_file, monkeypatch):
    from m3_core.paths import resolve_backend_name

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    assert resolve_backend_name() == "sqlite"


def test_env_is_read_live_after_the_snapshot(postgres_in_file, monkeypatch):
    """The snapshot is invalidated by the file, not by env: an env value present
    when it was built and removed since must not linger, and must not hide the
    file's value either."""
    from m3_core.paths import resolve_backend_name

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    assert resolve_backend_name() == "sqlite"  # the snapshot is built with env set
    monkeypatch.delenv("M3_DB_BACKEND")
    assert resolve_backend_name() == "postgres"


def test_a_flag_beats_env(postgres_in_file, monkeypatch):
    from m3_core.config import settings as st
    from m3_core.paths import resolve_backend_name

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    st.set_from_cli({"db.backend": "postgres"})
    assert resolve_backend_name() == "postgres"
