"""Every env name m3 reads is accounted for by the config registry (plan 2C.1).

A name the env gate (bin/check_env_access.py, the one inventory) finds in
production code must be one of:
  * an env name of a registry key (canonical or legacy alias);
  * declared not-a-setting, with the reason (keys.NOT_SETTINGS);
  * retired: documented but with no effect (keys.RETIRED);
  * a legacy store locator (config.stores.LEGACY_LOCATORS, plan 2C.3);
  * an OS / foreign name (the gate's own allowlist: not a finding at all);
  * listed in tests/data/config_unregistered.txt, the names still to register.

That list is a RATCHET: it may only shrink. A name registered or declared must
leave it, and a name the code no longer reads must leave it, so the file always
says exactly what is left. A NEW unaccounted name fails until it is registered.
"""

from __future__ import annotations

from pathlib import Path

_BASELINE = Path(__file__).with_name("data") / "config_unregistered.txt"


def _scanner_names() -> set[str]:
    import check_env_access

    findings, _unparsed = check_env_access.scan_repo()
    return {
        f.name
        for f in findings
        if f.verdict == "violation"
        and f.kind in ("read", "write")
        and f.name
        and not f.name.startswith("<")
        and f.name != "*"
    }


def _baseline() -> set[str]:
    lines = _BASELINE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def _accounted() -> set[str]:
    from m3_core.config.keys import NOT_SETTINGS, REGISTRY, RETIRED
    from m3_core.config.stores import LEGACY_LOCATORS

    return set(REGISTRY.env_names()) | set(NOT_SETTINGS) | set(RETIRED) | set(LEGACY_LOCATORS)


def test_every_env_name_is_registered_declared_or_listed():
    missing = sorted(_scanner_names() - _accounted() - _baseline())
    assert not missing, (
        "env names read by m3 that the config registry does not account for:\n  "
        + "\n  ".join(missing)
        + "\nregister each in bin/m3_core/config/keys.py (or, with a reason, "
        "keys.NOT_SETTINGS); never add a new name to the ratchet file"
    )


def test_the_ratchet_only_shrinks():
    baseline = _baseline()
    done = sorted(baseline & _accounted())
    gone = sorted(baseline - _scanner_names())
    assert not done, "now registered or declared; remove from tests/data/config_unregistered.txt:\n  " + "\n  ".join(
        done
    )
    assert not gone, (
        "no longer read by any production code; remove from tests/data/config_unregistered.txt:\n  " + "\n  ".join(gone)
    )


def test_the_ratchet_file_is_sorted_and_unique():
    names = [
        ln.strip() for ln in _BASELINE.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")
    ]
    assert names == sorted(set(names)), "keep tests/data/config_unregistered.txt sorted and unique"


def test_every_legacy_store_locator_is_read_and_accounted_once():
    """A declared locator no code reads is stale; one that is also a key or a
    not-setting is accounted twice, and the two reasons can disagree."""
    from m3_core.config.keys import NOT_SETTINGS, REGISTRY, RETIRED
    from m3_core.config.stores import LEGACY_LOCATORS

    stale = sorted(set(LEGACY_LOCATORS) - _scanner_names())
    assert not stale, "no longer read; remove from LEGACY_LOCATORS:\n  " + "\n  ".join(stale)
    twice = sorted(set(LEGACY_LOCATORS) & (set(REGISTRY.env_names()) | set(NOT_SETTINGS) | set(RETIRED)))
    assert not twice, "accounted elsewhere too:\n  " + "\n  ".join(twice)
