"""Roots resolve from the FILE, then the derived default, for the memory, engine, logs and models roots.
The former env names (M3_ENGINE_ROOT, M3_MEMORY_ROOT, ...) are not read.
``_roots`` reads ``[paths]`` itself with stdlib tomllib: it sits beside the OS seam
and must not import the registry above it."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conftest import write_sandbox_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _write_config(text: str) -> Path:
    return write_sandbox_config(text)


def test_a_root_set_in_the_file_wins_over_env(tmp_path):
    from m3_memory import _roots

    assert os.environ.get("M3_ENGINE_ROOT"), "the sandbox pins it; this test needs that"
    target = (tmp_path / "from-file").as_posix()
    _write_config(f'[paths]\nengine_root = "{target}"\nlogs_root = "{target}/logs"\n')
    assert Path(_roots.engine_root()) == Path(target)
    assert Path(_roots.logs_root()) == Path(target) / "logs"


def test_the_env_name_is_not_read(tmp_path, monkeypatch):
    from m3_memory import _roots

    target = (tmp_path / "from-file").as_posix()
    _write_config(f'[paths]\nengine_root = "{target}"\n')
    monkeypatch.setenv("M3_ENGINE_ROOT", (tmp_path / "from-env").as_posix())
    assert Path(_roots.engine_root()) == Path(target)
    _write_config("")  # the file names no engine root: still not the env's
    assert Path(_roots.engine_root()) != tmp_path / "from-env"


def test_no_file_and_no_env_gives_the_derived_default(monkeypatch):
    from m3_memory import _roots

    monkeypatch.setenv("M3_MODELS_ROOT", "/not/read")
    assert Path(_roots.models_root()) == Path(_roots.memory_root()) / "models"


def test_the_registry_and_roots_agree(tmp_path):
    """Two readers of the same keys (the cost of staying below the registry):
    whenever the file sets a root, both must answer the same."""
    from m3_core.config import settings as st

    from m3_memory import _roots

    target = (tmp_path / "agree").as_posix()
    _write_config(f'[paths]\nengine_root = "{target}"\n')
    st._reset_for_tests()
    assert Path(str(st.setting("paths.engine_root"))).resolve() == Path(_roots.engine_root()).resolve()


@pytest.mark.parametrize("value", ["42", '""'])
def test_a_malformed_root_is_loud(value):
    from m3_memory import _roots

    _write_config(f"[paths]\nengine_root = {value}\n")
    with pytest.raises(ValueError, match="engine_root"):
        _roots.engine_root()


def test_a_posix_root_reads_the_same_under_a_simulated_os(tmp_path, monkeypatch):
    """Strings through abs_root, never pathlib: a simulated POSIX run inside
    Windows must not trip "cannot instantiate PosixPath"."""
    from m3_memory import _roots

    _write_config('[paths]\nengine_root = "/data/m3/engine"\n')
    monkeypatch.setattr(sys, "platform", "linux")
    assert _roots.engine_root() == "/data/m3/engine"


def test_the_memory_root_in_the_file_wins_over_env(tmp_path, monkeypatch):
    """`[paths] memory_root` was read by the registry (and doctor) but not by
    _roots, which kept following M3_MEMORY_ROOT: doctor called the variable
    ignored while the audit trail followed it (2026-10-17). The roots derived from
    it follow the file too; the config root cannot (the file lives under it)."""
    from m3_memory import _roots

    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path / "from-env"))
    monkeypatch.delenv("M3_MODELS_ROOT", raising=False)
    config_before = _roots.config_root()
    target = (tmp_path / "from-file").as_posix()
    _write_config(f'[paths]\nmemory_root = "{target}"\n')
    assert Path(_roots.memory_root()) == Path(target)
    assert Path(_roots.models_root()) == Path(target) / "models"
    assert _roots.config_root() == config_before
