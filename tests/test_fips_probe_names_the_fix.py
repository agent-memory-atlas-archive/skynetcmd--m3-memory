"""A refused FIPS path is reported by name, with its fix.

The brief report said only "fips: tier mode; 1 path(s) refused", and warehouse
sync failed hourly behind it on two machines (2026-10-17): nothing named the
path or the repair.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def report(monkeypatch):
    import crypto_provider

    rows = [
        {"path": "embedding hash", "served_by": "wolfCrypt", "validated": False, "ok": True, "note": ""},
        {
            "path": "PostgreSQL (warehouse)",
            "served_by": "libpq",
            "validated": False,
            "ok": False,
            "note": "sslmode=unset (libpq default: prefer); FIPS tiers require verify-full",
            "remedy": crypto_provider._pg_verify_full_remedy(
                importlib.import_module("m3_core.secret_store").PgRole.WAREHOUSE
            ),
        },
    ]
    monkeypatch.setattr(
        crypto_provider, "crypto_paths_report", lambda: {"tier": "mode", "rows": rows, "os_fips_mode": None}
    )
    return importlib.import_module("doctor.fips_probe")


def test_brief_report_names_the_refused_path_and_its_fix(report, capsys):
    assert report.run(brief=True) == 1
    out = capsys.readouterr().out
    assert "1 path(s) refused" in out
    assert "PostgreSQL (warehouse)" in out and "sslmode=unset" in out
    assert "m3 secret set M3_CDW_PG_URL --keyring --stdin" in out
    assert "docs/TROUBLESHOOTING.md" in out


def test_the_fix_never_echoes_a_dsn(report, capsys):
    report.run(brief=False)
    out = capsys.readouterr().out
    assert "postgresql://" not in out and "@" not in out.split("fix:", 1)[-1]
