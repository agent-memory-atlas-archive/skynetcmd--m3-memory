"""Scheduled jobs must find the same config root as the process that registered
them. A task runs with none of the registering shell's environment (launchd,
systemd, Task Scheduler, cron), so a NON-default config root, chosen by
`--config` or by M3_CONFIG_ROOT, reaches the job only as a `--config` argument
in its definition. Without it the MCP server and hooks (pinned) and the
scheduled jobs (defaults) split across two stores."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

_REPO = Path(__file__).resolve().parents[1]


def _config_file() -> str:
    from m3_memory import _roots

    return _roots.config_file()


def test_the_sandbox_root_is_not_the_default():
    """These tests rely on the sandbox's non-default M3_CONFIG_ROOT."""
    from m3_memory import _roots

    assert _roots.config_root_with_tier()[1] != _roots.CONFIG_ROOT_DEFAULT


def test_every_task_spec_carries_the_config_file():
    import install_schedules

    specs = install_schedules.get_schedule_specs(str(_REPO))
    assert specs
    missing = [s["name"] for s in specs if ["--config", _config_file()] != _pair(s["args"])]
    assert not missing, f"tasks that would run against the DEFAULT config root: {missing}"


def _pair(args: list[str]) -> list[str]:
    i = args.index("--config") if "--config" in args else -1
    return args[i : i + 2] if i >= 0 else []


@pytest.mark.parametrize(
    "template",
    sorted(p.name for p in (_REPO / "bin").iterdir() if p.suffix in (".plist", ".service")),
)
def test_every_service_template_carries_the_config_file(template):
    import install_schedules

    text = install_schedules._render_template(str(_REPO / "bin" / template), str(_REPO), "python", port=8088)
    assert _config_file() in text, f"{template} renders without --config"


def test_the_early_pin_reaches_paths_bound_at_import(tmp_path):
    """chatlog_config binds its paths AT IMPORT; an argparse pin would be too late.
    A child that pins from argv first (as every scheduled entry script does) gets
    them under the pinned root."""
    import subprocess

    pinned = tmp_path / "pinned" / "m3_config.toml"
    code = (
        "import sys; sys.argv=['job','--config',sys.argv[1]]\n"
        "from m3_core.seams import roots; roots.pin_from_argv()\n"
        "import chatlog_config; print(chatlog_config.CONFIG_PATH)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(pinned)],
        cwd=str(_REPO / "bin"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()).parent == pinned.parent, out.stdout


@pytest.mark.parametrize(
    "script",
    [
        "weekly_auditor",
        "sync_all",
        "memory_maintenance",
        "secret_rotator",
        "chatlog_embed_sweeper",
        "m3_enrich",
        "m3_cognitive_loop",
        "embed_server_inproc",
        "m3_notification_waiter",
        "dashboard_server",
        "chatlog_init",
        "install_schedules",
    ],
)
def test_every_entry_script_accepts_the_flag(script, tmp_path):
    """argparse must declare --config, or the task fails to start on the new args."""
    import subprocess

    out = subprocess.run(
        [sys.executable, str(_REPO / "bin" / f"{script}.py"), "--config", str(tmp_path / "m3_config.toml"), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-500:]


def test_the_default_root_adds_nothing(monkeypatch):
    """A default install keeps today's task definitions byte for byte."""
    import install_schedules

    from m3_memory import _roots

    monkeypatch.setattr(_roots, "config_root_with_tier", lambda: ("/home/u/.m3/config", _roots.CONFIG_ROOT_DEFAULT))
    specs = install_schedules.get_schedule_specs(str(_REPO))
    assert not [s["name"] for s in specs if "--config" in s["args"]]


def test_a_variable_naming_the_default_root_pins_nothing(tmp_path, monkeypatch):
    """M3_CONFIG_ROOT set to the default folder is the default: pinning it changed
    every job definition, and re-registering a Windows boot task then needed an
    administrator (measured on an upgrade, 2026-10-15)."""
    from m3_memory import _roots

    default = tmp_path / ".m3" / "config"
    default.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(_roots, "_home", lambda *parts: str(tmp_path.joinpath(*parts)))
    for spelling, pinned in ((str(default), False), (default.as_posix() + "/", False), (str(other), True)):
        tier = (spelling, _roots.CONFIG_ROOT_ENV)
        monkeypatch.setattr(_roots, "config_root_with_tier", lambda tier=tier: tier)
        assert bool(_roots.located_config_args()) is pinned, spelling
