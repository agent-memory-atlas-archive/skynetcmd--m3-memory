"""The built package carries every schema chain under memory/.

Installed m3 finds its migrations inside the package (m3_memory/memory/<chain>).
A hand-kept list in setup.py once named only two of the four chains, so an
installed m3's `m3 doctor --fix` failed with "no migrations found in
.../m3_memory/memory/dispatch_migrations" and the warehouse chain was missing too.
Every test ran from a source checkout, where the directories exist, so nothing
caught it. This checks the mapping the wheel is built from, and the sdist manifest
the wheel is rebuilt from on `pip install <sdist>`.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _setup_namespace():
    """setup.py's helpers, without running setup()."""
    import os

    src = (ROOT / "setup.py").read_text(encoding="utf-8")
    head = src.split("_pkg_dir, _payload_pkgs, _pkg_data = _payload_mapping()")[0]
    ns: dict = {}
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        exec(compile(head, "setup.py", "exec"), ns)  # noqa: S102 — the repo's own build script
        return ns, ns["_payload_mapping"]()
    finally:
        os.chdir(cwd)


def _chains_on_disk():
    return sorted(p.name for p in (ROOT / "memory").iterdir() if p.is_dir() and p.name.endswith("migrations"))


def test_every_chain_on_disk_is_in_the_wheel_mapping():
    _ns, (_dirs, packages, _data) = _setup_namespace()
    shipped = {p.rsplit(".", 1)[-1] for p in packages if p.count(".") == 2 and p.startswith("m3_memory.memory.")}
    missing = set(_chains_on_disk()) - shipped
    assert not missing, f"schema chains not shipped in the wheel: {sorted(missing)}"


def test_the_sdist_manifest_keeps_every_chains_sql():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    covered = "recursive-include memory *.sql" in manifest
    per_chain = all(f"recursive-include memory/{c} *.sql" in manifest for c in _chains_on_disk())
    assert covered or per_chain, "MANIFEST.in drops some memory/*migrations SQL from the sdist"
