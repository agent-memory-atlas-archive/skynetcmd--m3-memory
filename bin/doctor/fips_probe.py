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


def run(brief: bool = False) -> int:
    try:
        from crypto_provider import crypto_paths_report
    except Exception as e:  # noqa: BLE001 — report what blocked the probe, never crash the doctor
        print(f"fips: report unavailable ({type(e).__name__}: {e})")
        return 0
    report = crypto_paths_report()
    tier = report["tier"]
    refused = [r for r in report["rows"] if not r["ok"]]
    if brief:
        if tier != "off" or refused:
            state = f"{len(refused)} path(s) refused" if refused else "all paths accepted"
            print(f"fips: tier {tier}; {state}")
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
    print("  m3 is FIPS 140-3 ready, not validated: validation belongs to the modules above in an assessed")
    print("  deployment. Profiles and remedies: docs/FIPS_MODULE_BOUNDARY.md")
    return 1 if refused and tier != "off" else 0
