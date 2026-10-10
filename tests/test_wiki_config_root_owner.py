"""bin/wiki resolves the config root through m3_core.paths.get_m3_config_root.

Four hand-copied resolvers (admission, authority, pii_scrub, select) had drifted
from the owner: they skipped its _abs_root step.
"""
from __future__ import annotations

from pathlib import Path

_WIKI = Path(__file__).resolve().parents[1] / "bin" / "wiki"


def test_wiki_never_reads_the_root_env_vars_itself():
    offenders = [f"{p.name}:{n}" for p in sorted(_WIKI.rglob("*.py"))
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if "environ" in line and ("M3_CONFIG_ROOT" in line or "M3_MEMORY_ROOT" in line)]
    assert not offenders, f"resolve the config root via get_m3_config_root, not by hand: {offenders}"
