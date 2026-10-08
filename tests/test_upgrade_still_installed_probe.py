"""`m3_upgrade.m3_still_installed` must not count an import from the working directory.

After a failed upgrade step it asks the OWNING interpreter whether m3 still
imports. `-c` puts the working directory on sys.path, and the suite (like a
developer running the upgrade from a checkout) runs from a repo root that
contains m3_memory/, so a probe that inherits the cwd reported a removed m3 as
installed. These tests run from that root, which makes them the planted case.
"""
from __future__ import annotations

import os
import pathlib
import sys
import venv

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

import m3_upgrade as m3u  # noqa: E402


def _python(env_dir: pathlib.Path) -> str:
    return str(env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def test_a_venv_without_m3_is_not_reported_installed(tmp_path, monkeypatch):
    monkeypatch.chdir(pathlib.Path(__file__).resolve().parents[1])  # holds m3_memory/
    venv.create(tmp_path / "bare", with_pip=False, system_site_packages=False)
    assert m3u.m3_still_installed(_python(tmp_path / "bare")) is False


def test_a_missing_interpreter_is_unknown_not_installed(tmp_path):
    assert m3u.m3_still_installed(str(tmp_path / "gone" / "python")) is None
