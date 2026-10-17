"""The security (FIPS tier) seam: one owner of the rules, and its gates.

Seam hierarchy: version < OS < security < database < logic (m3 memory
`f5aecb25`; DESIGN_PHILOSOPHIES §10a). The rules live in bin/crypto_policy.py,
the backend in bin/crypto_provider.py. The defect this seam exists for:
crypto_provider read the tier from the environment only and the Rust embed
server from its own file, so `[crypto] fips_mode = true` in m3_config.toml left
a daemon silently non-FIPS; doctor carried its own copy of the tier rule.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

from conftest import skip_reason

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "bin" / "crypto_policy.py"
_READ = re.compile(r"""setting\(\s*["']crypto\.""")


def _policy():
    return importlib.import_module("crypto_policy")


def test_only_the_rules_module_reads_crypto_settings():
    """A second reader is how FIPS from the config file went missing."""
    offenders = []
    for base in ("bin", "m3_memory"):
        for p in sorted((ROOT / base).rglob("*.py")):
            if "__pycache__" in p.parts or p == POLICY:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            for n, line in enumerate(text.splitlines(), 1):
                if _READ.search(line):
                    offenders.append(f"{p.relative_to(ROOT)}:{n}")
    assert not offenders, (
        "crypto.* settings are read only by bin/crypto_policy.py (fips_tier, crypto_setting, "
        "transport_allowed); call it instead:\n  " + "\n  ".join(offenders)
    )


def test_the_rules_module_loads_no_crypto_library():
    """It must import when crypto_provider fails closed (FIPS without wolfSSL):
    stdlib and the config package only, no crypto backend, no database layer."""
    tree = ast.parse(POLICY.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    import sys

    foreign = sorted(
        m
        for m in imported
        if m.split(".")[0] not in sys.stdlib_module_names and m != "__future__" and not m.startswith("m3_core.config")
    )
    assert not foreign, f"crypto_policy may import only the stdlib and m3_core.config: {foreign}"


@pytest.mark.parametrize(
    ("values", "tier"),
    [
        ({}, "off"),
        ({"crypto.fips_mode": True}, "mode"),
        ({"crypto.fips_strict": True}, "strict"),
        ({"crypto.fips_mode": True, "crypto.fips_strict": True}, "strict"),
    ],
)
def test_the_tier_rule(m3_settings, values, tier):
    m3_settings(values)
    assert _policy().fips_tier() == tier


def test_third_party_services_only_outside_fips(m3_settings):
    pol = _policy()
    m3_settings({})
    assert pol.transport_allowed("third_party_service") == (True, "")
    m3_settings({"crypto.fips_mode": True})
    ok, why = pol.transport_allowed("third_party_service")
    assert not ok and "crypto.fips_mode" in why


def test_strict_needs_the_tls_attestation(m3_settings):
    pol = _policy()
    m3_settings({"crypto.fips_strict": True})
    ok, why = pol.transport_allowed("network_tls")
    assert not ok and "crypto.tls_validated" in why
    m3_settings({"crypto.fips_strict": True, "crypto.tls_validated": True})
    assert pol.transport_allowed("network_tls") == (True, "")
    m3_settings({"crypto.fips_mode": True})
    assert pol.transport_allowed("network_tls") == (True, "")


def test_an_unknown_transport_is_refused_not_guessed():
    with pytest.raises(ValueError, match="unknown transport kind"):
        _policy().transport_allowed("carrier_pigeon")


def test_the_provider_re_exports_the_rules_not_a_copy():
    cp = importlib.import_module("crypto_provider")
    pol = _policy()
    assert cp.fips_tier is pol.fips_tier
    assert cp.transport_allowed is pol.transport_allowed
    assert cp.FipsTransportError is pol.FipsTransportError


@pytest.mark.parametrize(
    ("toml", "tier"),
    [
        ("", "off"),
        ("[crypto]\nfips_mode = true\n", "mode"),
        ("[crypto]\nfips_strict = true\n", "strict"),
        ("[crypto]\nfips_mode = true\nfips_strict = false\n", "mode"),
    ],
)
def test_python_and_rust_compute_the_same_tier(tmp_path, monkeypatch, toml, tier):
    """The same m3_config.toml gives the same tier in both languages."""
    core = pytest.importorskip("m3_core_rs", reason=skip_reason("dep", "m3_core_rs not installed"))
    if not hasattr(core, "fips_tier_from_config"):
        pytest.skip(skip_reason("dep", "this m3_core_rs predates fips_tier_from_config (3.10.16)"))
    cfg = tmp_path / "m3_config.toml"
    cfg.write_text("schema_version = 1\n" + toml, encoding="utf-8")
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    for name in ("M3_FIPS_MODE", "M3_FIPS_STRICT"):
        monkeypatch.delenv(name, raising=False)
    st = importlib.import_module("m3_core.config.settings")
    st._reset_for_tests()
    try:
        assert _policy().fips_tier() == tier
        assert core.fips_tier_from_config(str(cfg)) == tier
    finally:
        st._reset_for_tests()
