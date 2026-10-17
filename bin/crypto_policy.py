"""The security seam's rules: the FIPS tier and which transports it allows.

m3's seam hierarchy is version < OS < security < database < logic (m3 memory
`f5aecb25`). The security seam has two halves: this module, the RULES, and
`crypto_provider`, the BACKEND (wolfCrypt or hashlib, self-tests). The rules
live apart because `crypto_provider` loads its backend at import and fails
closed in FIPS mode without wolfSSL; anything that must still answer "what
does the configured tier allow?" (doctor, the transport checks) imports this
module, which loads no crypto library. Rust's counterpart is
`m3_config::security`; a parity test holds the two to the same tier.

Every [crypto] setting is read here, through the config registry, never from
the environment alone (a daemon has no shell env). Nothing else reads them.
"""

from __future__ import annotations

FIPS_TIERS = ("off", "mode", "strict")

# What `transport_allowed` can be asked about. A new kind is added here, with its
# rule, never decided at a call site.
TRANSPORTS = frozenset(
    {
        "network_tls",  # a TLS connection to another machine (Python's ssl / libpq)
        "third_party_service",  # a chat or cloud service outside the user's machines (Discord, Matrix, Slack)
    }
)


class FipsTransportError(RuntimeError):
    """A network connection a FIPS tier does not allow, with the remedy."""


def crypto_setting(name: str) -> object:
    """THE reader of every [crypto] setting: the config registry, which also
    honours each key's legacy env name. No fallback: a setting that cannot be
    read raises rather than reading as off."""
    from m3_core.config import settings as _cfg

    return _cfg.setting(name)


def crypto_text(name: str) -> str:
    return str(crypto_setting(name) or "").strip()


def legacy_env_names() -> frozenset[str]:
    """Every env name a [crypto] setting still answers to (below the file). The
    test sandbox clears them: an exported M3_FIPS_MODE otherwise sets the tier
    for every test, and the suite passes or fails by the developer's shell."""
    from m3_core.config import REGISTRY

    return frozenset(n for k in REGISTRY if k.name.startswith("crypto.") for n in k.env_names)


def fips_mode() -> bool:
    return bool(crypto_setting("crypto.fips_mode"))


def fips_strict() -> bool:
    # STRICT implies MODE; setting STRICT alone also engages the wolfSSL backend.
    return bool(crypto_setting("crypto.fips_strict"))


def fips_active() -> bool:
    """Any FIPS tier (mode or strict): the FIPS-ready backend is required."""
    return fips_mode() or fips_strict()


def fips_tier() -> str:
    """The active tier: "strict", "mode" or "off" (strict implies mode)."""
    return "strict" if fips_strict() else ("mode" if fips_mode() else "off")


def tls_validated_attested() -> bool:
    """The operator's attestation that network TLS runs on a validated module
    (crypto.tls_validated). An unreadable setting counts as no attestation:
    strict then refuses, the safe side."""
    try:
        return bool(crypto_setting("crypto.tls_validated"))
    except Exception:  # noqa: BLE001 — no readable setting means no attestation
        return False


def transport_allowed(kind: str) -> tuple[bool, str]:
    """Whether the configured tier allows a transport of ``kind``, and why not.
    Returns (True, "") or (False, <reason with the setting to change>). An
    unknown kind raises: a call site must not invent a rule."""
    if kind not in TRANSPORTS:
        raise ValueError(f"unknown transport kind {kind!r}; known: {', '.join(sorted(TRANSPORTS))}")
    tier = fips_tier()
    if kind == "third_party_service":
        if tier == "off":
            return True, ""
        return False, (
            f"not available under the FIPS tier ({tier}): messages would leave this machine for a "
            "service outside m3's FIPS boundary. setting: crypto.fips_mode / crypto.fips_strict"
        )
    # network_tls
    if tier == "strict" and not tls_validated_attested():
        return False, (
            "crypto.fips_strict refuses network TLS until crypto.tls_validated attests that the TLS library "
            "is a FIPS 140-3 validated module (docs/FIPS_MODULE_BOUNDARY.md)"
        )
    return True, ""
