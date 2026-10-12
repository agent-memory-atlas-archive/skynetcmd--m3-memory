"""`m3 --config FILE`: the process reads FILE and its directory is the config root,
above M3_CONFIG_ROOT (CONFIG_DESIGN §9); m3 children get `--config`, never env (§5)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def test_the_pin_outranks_the_env_root(tmp_path):
    from m3_core.config.loader import CONFIG_ARG_TIER, locate

    from m3_memory import _roots

    assert os.environ.get("M3_CONFIG_ROOT"), "the sandbox pins it; this test needs that"
    f = tmp_path / "elsewhere" / "custom.toml"
    _roots.pin_config_file(f)
    root, tier = _roots.config_root_with_tier()
    assert (Path(root), tier) == (f.parent, CONFIG_ARG_TIER)
    loc = locate()
    assert (loc.file, loc.tier) == (f, CONFIG_ARG_TIER), "a file not named m3_config.toml still answers"
    assert _roots.config_args() == ["--config", f.as_posix()]


def test_a_posix_pin_reads_the_same_under_a_simulated_os(monkeypatch):
    """Strings through abs_root, never pathlib: a POSIX path pinned during a
    simulated Linux run inside Windows keeps its own convention."""
    from m3_memory import _roots

    monkeypatch.setattr(sys, "platform", "linux")
    _roots.pin_config_file("/srv/m3/config/m3_config.toml")
    assert _roots.config_root() == "/srv/m3/config"


def test_the_settings_snapshot_follows_a_pin_set_after_it(tmp_path):
    from m3_core.config import settings as st

    st._reset_for_tests()
    assert st.setting("search.row_cap") != 7
    f = tmp_path / "m3_config.toml"
    f.write_text("schema_version = 1\n[search]\nrow_cap = 7\n", encoding="utf-8")
    from m3_memory import _roots

    _roots.pin_config_file(f)
    assert st.setting("search.row_cap") == 7


def test_m3_children_get_the_flag_and_the_parent_env_is_untouched(tmp_path):
    from m3_memory import _roots, setup_wizard

    before = dict(os.environ)
    _roots.pin_config_file(tmp_path / "m3_config.toml")
    argv = setup_wizard._m3_cli("doctor")
    assert argv[4:6] == ["--config", (tmp_path / "m3_config.toml").as_posix()], argv
    assert dict(os.environ) == before


def test_the_cli_flag_pins_the_file(tmp_path):
    """End to end through the real entry point: `m3 --config X config show` reads X."""
    f = tmp_path / "m3_config.toml"
    f.write_text("schema_version = 1\n[search]\nrow_cap = 7\n", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", "--config", str(f), "config", "show", "--origin"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "row_cap" in out.stdout and "7" in out.stdout and f.parent.name in out.stdout, out.stdout


def test_the_conftest_guard_clears_process_flags():
    """The guard must be able to fail: leave both kinds of process flag set, run
    it, and see both gone."""
    from m3_core.config import settings as st

    from conftest import clear_process_flags
    from m3_memory import _roots

    _roots.pin_config_file("/tmp/x/m3_config.toml")
    st.set_from_cli({"db.backend": "postgres"})
    assert _roots.pinned_config_file() is not None and st._CACHE._cli
    clear_process_flags()
    assert _roots.pinned_config_file() is None and not st._CACHE._cli


def test_the_dsn_leak_guard_can_fail():
    """A raw os.environ write of a DSN name is detected and undone."""
    from conftest import dsn_env_snapshot, restore_dsn_env

    before = dsn_env_snapshot()
    os.environ["M3_PG_URL"] = "postgresql://guard-selftest/x"
    try:
        assert restore_dsn_env(before) == ["M3_PG_URL"]
    finally:
        restore_dsn_env(before)
    assert os.environ.get("M3_PG_URL") == before["M3_PG_URL"]


def test_setup_records_decoupled_roots_in_the_file_not_env(tmp_path):
    """The wizard's roots row: the chosen config file is pinned, the engine root
    goes into its [paths], every root reader follows, and env is untouched."""
    from m3_memory import _roots, setup_wizard

    before = dict(os.environ)
    cfg, eng = (tmp_path / "cfg").as_posix(), (tmp_path / "eng").as_posix()
    os.makedirs(cfg)
    assert setup_wizard._record_decoupled_roots(cfg, eng) is True
    assert dict(os.environ) == before, "no env write"
    assert (Path(_roots.config_root()), Path(_roots.engine_root())) == (Path(cfg), Path(eng))
    assert "engine_root" in (Path(cfg) / "m3_config.toml").read_text(encoding="utf-8")
    # Script children (install_schedules, chatlog_init) get the file as an argument.
    assert setup_wizard._job_config_args() == ["--config", f"{cfg}/m3_config.toml"]
