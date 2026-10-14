"""conftest.use_backend selects the backend the way a user does: m3_config.toml
and the secrets file, never an env var."""

from __future__ import annotations

import os

from conftest import use_backend


def test_sqlite_is_selected_through_the_config_file():
    from m3_core.config import locate
    from m3_core.paths import resolve_backend_name

    use_backend("sqlite")
    assert resolve_backend_name() == "sqlite"
    assert 'backend = "sqlite"' in locate().file.read_text(encoding="utf-8")
    assert "M3_DB_BACKEND" not in os.environ


def test_postgres_and_its_dsn_come_from_the_files(monkeypatch):
    from m3_core.paths import resolve_backend_name
    from m3_core.secret_store import PgRole, explain_pg_dsn, secrets_file_path

    # A PG lane provides its cluster through this until step 5; remove it here
    # so the answer can only come from the secrets file.
    monkeypatch.delenv("M3_PRIMARY_PG_URL", raising=False)
    use_backend("postgres", "postgresql://u@h/throwaway")
    assert resolve_backend_name() == "postgres"
    got = explain_pg_dsn(PgRole.PRIMARY)
    assert got.dsn == "postgresql://u@h/throwaway" and got.source.startswith("secrets-file:"), got
    assert "M3_PRIMARY_PG_URL" in secrets_file_path().read_text(encoding="utf-8")
