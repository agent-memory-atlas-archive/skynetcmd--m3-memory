"""Roots resolve FILE first, then the env var read LIVE, then the derived default
(user decision 2026-10-11, m3 d4ea1da9), for the engine, logs and models roots.
``_roots`` reads ``[paths]`` itself with stdlib tomllib: it sits beside the OS seam
and must not import the registry above it."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _write_config(text: str) -> Path:
    from m3_memory import _roots

    root = Path(_roots.config_root())
    root.mkdir(parents=True, exist_ok=True)
    f = root / "m3_config.toml"
    f.write_text("schema_version = 1\n" + text, encoding="utf-8")
    return f


def test_a_root_set_in_the_file_wins_over_env(tmp_path):
    from m3_memory import _roots

    assert os.environ.get("M3_ENGINE_ROOT"), "the sandbox pins it; this test needs that"
    target = (tmp_path / "from-file").as_posix()
    _write_config(f'[paths]\nengine_root = "{target}"\nlogs_root = "{target}/logs"\n')
    assert Path(_roots.engine_root()) == Path(target)
    assert Path(_roots.logs_root()) == Path(target) / "logs"


def test_env_is_read_live(tmp_path, monkeypatch):
    from m3_memory import _roots

    moved = (tmp_path / "moved").as_posix()
    monkeypatch.setenv("M3_ENGINE_ROOT", moved)
    assert Path(_roots.engine_root()) == Path(moved)


def test_no_file_and_no_env_gives_the_derived_default(monkeypatch):
    from m3_memory import _roots

    monkeypatch.delenv("M3_MODELS_ROOT", raising=False)
    assert Path(_roots.models_root()) == Path(os.environ["M3_MEMORY_ROOT"]) / "models"


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
