"""fips_probe: an unloadable crypto provider fails the doctor under a FIPS tier,
and stays informational outside one (bin/doctor/fips_probe.py)."""

from __future__ import annotations

import builtins
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from doctor import fips_probe  # noqa: E402


@pytest.fixture
def no_crypto_provider(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "crypto_provider":
            raise ImportError("libwolfssl not found")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)


@pytest.mark.parametrize(("tier", "rc"), [("off", 0), ("mode", 1), ("strict", 1), (None, 1)])
def test_report_unavailable_fails_only_outside_off(no_crypto_provider, monkeypatch, capsys, tier, rc):
    monkeypatch.setattr(fips_probe, "_configured_fips_tier", lambda: tier)
    assert fips_probe.run(brief=True) == rc
    out = capsys.readouterr().out
    assert "libwolfssl not found" in out
    assert f"configured tier: {tier or 'unknown'}" in out
