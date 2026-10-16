"""[crypto] settings take effect when they come only from the config, not the env.

crypto_provider read M3_FIPS_MODE / M3_FIPS_STRICT / M3_WOLFSSL_LIB /
M3_WOLFSSL_SHA256 / M3_CRYPTO_BACKEND from the process environment alone, so
`[crypto] fips_mode = true` in m3_config.toml was ignored: a daemon (launchd,
systemd, Task Scheduler) configured by the file ran on the default backend,
silently outside FIPS mode. m3_settings installs a snapshot with no process env,
so these pass only if the provider reads the registry.
"""

from __future__ import annotations

import importlib
import os

import pytest


def _provider():
    return importlib.import_module("crypto_provider")


def test_fips_mode_from_config_alone(m3_settings):
    cp = _provider()
    m3_settings({"crypto.fips_mode": True})
    assert cp.fips_tier() == "mode"
    m3_settings({"crypto.fips_mode": True, "crypto.fips_strict": True})
    assert cp.fips_tier() == "strict"
    m3_settings({})
    assert cp.fips_tier() == "off"


def test_wolfssl_pins_from_config_alone(m3_settings, tmp_path):
    cp = _provider()
    lib = tmp_path / "wolfssl.dll"
    lib.write_bytes(b"the-real-bytes")
    m3_settings({"crypto.wolfssl_lib": str(lib), "crypto.wolfssl_sha256": "00" * 32})
    assert cp._trusted_wolfssl_candidates()[0] == os.path.abspath(str(lib))
    with pytest.raises(RuntimeError, match="integrity check failed"):
        cp._resolve_wolfssl_path()
