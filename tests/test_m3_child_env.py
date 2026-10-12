"""Setup's m3 children get this package's root on PYTHONPATH per spawn, never via
a global write: ``_m3_cli`` used to put it into ``os.environ`` on its first call,
so every later child of setup (pip, schtasks) inherited it by accident."""

from __future__ import annotations

import os
from pathlib import Path


def test_building_an_m3_argv_leaves_the_environment_alone(monkeypatch):
    from m3_memory import setup_wizard

    monkeypatch.setenv("PYTHONPATH", "parent")
    before = dict(os.environ)
    setup_wizard._m3_cli("doctor")
    assert dict(os.environ) == before


def test_the_m3_child_env_puts_this_package_first(monkeypatch):
    from m3_memory import setup_wizard

    monkeypatch.setenv("PYTHONPATH", "parent")
    env = setup_wizard._m3_child_env()
    root = str(Path(setup_wizard.__file__).resolve().parent.parent)
    assert env["PYTHONPATH"].split(os.pathsep) == [root, "parent"]
    assert os.environ["PYTHONPATH"] == "parent", "the parent environment is never modified"
