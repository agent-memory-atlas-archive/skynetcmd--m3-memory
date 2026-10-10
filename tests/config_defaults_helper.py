"""Shared by the per-table registry tests: what today's readers produce.

Defaults are bound to module constants at IMPORT, so an in-process import can
hold an earlier test's values. ``todays_values`` imports the modules in a FRESH
interpreter with only the measured env names removed (the rest, e.g. the
sandbox's M3_LIB_DIR on a FIPS-mode box, must stay or the import fails for an
unrelated reason) and returns {registry key: today's value}.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"


def todays_values(pinned: dict[str, tuple[str, str]]) -> dict:
    from m3_core.config import REGISTRY

    measured = {n for k in pinned for n in REGISTRY.get(k).env_names}
    env = {k: v for k, v in os.environ.items() if k not in measured}
    prog = (
        "import importlib, json, sys\n"
        f"sys.path.insert(0, {str(_BIN)!r})\n"
        f"pinned = {json.dumps(pinned)}\n"
        "out = {}\n"
        "for key, (mod, attr) in pinned.items():\n"
        "    v = getattr(importlib.import_module(mod), attr)\n"
        # A set's order is arbitrary: mark it, so it is compared as a set.
        "    out[key] = {'set': sorted(v)} if isinstance(v, (frozenset, set)) else (list(v) if isinstance(v, tuple) else v)\n"
        "print(json.dumps(out))\n"
    )
    r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def mismatches(pinned: dict[str, tuple[str, str]]) -> dict:
    """{key: (registry default, today's value)} for every key that disagrees."""
    from m3_core.config import REGISTRY

    today = todays_values(pinned)
    out = {}
    for k, v in today.items():
        default = REGISTRY.get(k).default
        if isinstance(v, dict) and set(v) == {"set"}:
            if sorted(default or []) != v["set"]:
                out[k] = (default, v["set"])
        elif default != v:
            out[k] = (default, v)
    return out
