"""FIPS posture, path by path: what serves each cryptographic operation m3
performs, whether that is a validated module, and whether the active tier
accepts it (crypto_provider.crypto_paths_report owns the facts).

Exit code: under a FIPS tier, a path the tier refuses is a real failure (the
connection will be refused at runtime), so it counts. Outside FIPS tiers the
report is informational and never fails the doctor.
"""

from __future__ import annotations


def _mark(validated) -> str:
    return {True: "validated", False: "not validated", None: "operator attests"}[validated]


def _configured_fips_tier() -> str | None:
    """The configured tier from the security seam's rules (crypto_policy), which
    load no crypto library, so this works when crypto_provider cannot import.
    None when the settings cannot be read."""
    try:
        from crypto_policy import fips_tier

        return fips_tier()
    except Exception:  # noqa: BLE001 -- caller treats unknown as a failure
        return None


def run(brief: bool = False) -> int:
    try:
        from crypto_provider import crypto_paths_report
    except Exception as e:  # noqa: BLE001 — report what blocked the probe, never crash the doctor
        # Under a FIPS tier an unloadable crypto provider means every crypto path
        # fails at runtime: a failure, not a note. Unknown tier fails closed.
        tier = _configured_fips_tier()
        print(f"fips: report unavailable ({type(e).__name__}: {e}); configured tier: {tier or 'unknown'}")
        if tier == "off":
            return 0
        print(
            "  fix: resolve the import error above (wolfSSL is installed by `m3 setup`); docs/FIPS_MODULE_BOUNDARY.md"
        )
        return 1
    report = crypto_paths_report()
    tier = report["tier"]
    refused = [r for r in report["rows"] if not r["ok"]]
    if brief:
        if tier != "off" or refused:
            state = f"{len(refused)} path(s) refused" if refused else "all paths accepted"
            print(f"fips: tier {tier}; {state}")
            # Each refused path by name, with its fix: a bare count sent no one
            # anywhere (warehouse sync failed hourly behind "1 path(s) refused").
            if tier != "off":
                for r in refused:
                    print(f"  ✗ {r['path']}: {r['note']}" + (f"\n    fix: {r['remedy']}" if r.get("remedy") else ""))
        return 1 if refused and tier != "off" else 0
    print()
    print("=== FIPS 140-3 posture ===")
    print(f"  tier: {tier}   (off | mode = M3_FIPS_MODE | strict = M3_FIPS_STRICT)")
    osf = report["os_fips_mode"]
    print(f"  OS FIPS mode: {'on' if osf else ('off' if osf is False else 'not applicable')}")
    for r in report["rows"]:
        flag = "OK  " if r["ok"] else "FAIL"
        print(f"  [{flag}] {r['path']}")
        print(f"         {r['served_by']}  ({_mark(r['validated'])})" + (f"  {r['note']}" if r["note"] else ""))
        if not r["ok"] and r.get("remedy"):
            print(f"         fix: {r['remedy']}")
    print("  m3 is FIPS 140-3 ready, not validated: validation belongs to the modules above in an assessed")
    print("  deployment. Profiles and remedies: docs/FIPS_MODULE_BOUNDARY.md")
    return 1 if refused and tier != "off" else 0
