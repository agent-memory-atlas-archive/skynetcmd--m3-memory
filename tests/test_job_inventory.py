"""m3 doctor lists every m3 background job with a verdict, and repair removes only
what is safe to remove (doctor/job_inventory). Hermetic: fake LaunchAgents and
systemd directories, fake installs with their own dist-info, stubbed schedulers."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _install(base: Path, version: str) -> Path:
    """A Python environment holding m3 ``version``; returns its python."""
    (base / "bin").mkdir(parents=True)
    (base / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (base / "lib" / "python3.12" / "site-packages" / f"m3_memory-{version}.dist-info").mkdir(parents=True)
    py = base / "bin" / "python"
    py.write_text("", encoding="utf-8")
    return py


def _agent(agents: Path, label: str, program, script) -> None:
    args = [str(program)] + ([str(script)] if script else [])
    (agents / f"{label}.plist").write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": args}))


@pytest.fixture
def mac(tmp_path, monkeypatch):
    import install_schedules as sched
    from doctor import job_inventory as ji

    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    this_py = _install(tmp_path / "this", "2026.10.15.0")
    for name in ("m3-cognitive-loop", "m3-dashboard", "m3-loop-watchdog"):
        (this_py.parent / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(ji, "_LAUNCH_AGENTS", str(agents))
    monkeypatch.setattr(ji, "is_windows", lambda: False)
    monkeypatch.setattr(ji, "is_macos", lambda: True)
    monkeypatch.setattr(ji, "_cron_jobs", list)
    monkeypatch.setattr(ji, "_this_version", lambda: "2026.10.15.0")
    monkeypatch.setattr(ji, "_taken_over_by_loop", set)
    monkeypatch.setattr(ji, "_backup_dir", lambda: str(tmp_path / "backup"))
    (tmp_path / "backup").mkdir()
    monkeypatch.setattr(sched, "_venv_python", lambda root, windowless=False: str(this_py))
    calls = []
    monkeypatch.setattr(ji, "_run", lambda argv, **k: calls.append(argv))
    if not hasattr(os, "getuid"):
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    return ji, agents, this_py, tmp_path, calls


def _by_ident(jobs):
    return {j.ident: j for j in jobs}


def test_each_m3_job_gets_a_verdict(mac):
    ji, agents, this_py, tmp, _calls = mac
    old_py = _install(tmp / "old", "2026.10.4.0")
    new_py = _install(tmp / "new", "2027.1.0")
    (old_py.parent / "m3-dashboard").write_text("", encoding="utf-8")
    (new_py.parent / "m3-loop-watchdog").write_text("", encoding="utf-8")
    bin_ = ROOT / "bin"
    _agent(agents, "com.m3memory.cognitiveloop", this_py.parent / "m3-cognitive-loop", bin_ / "m3_cognitive_loop.py")
    _agent(agents, "com.m3memory.dashboard", old_py.parent / "m3-dashboard", tmp / "old" / "dashboard_server.py")
    (tmp / "old" / "dashboard_server.py").write_text("", encoding="utf-8")
    _agent(agents, "com.m3memory.loopwatchdog", new_py.parent / "m3-loop-watchdog", tmp / "new" / "m3_loop_watchdog.py")
    (tmp / "new" / "m3_loop_watchdog.py").write_text("", encoding="utf-8")
    _agent(agents, "com.m3memory.gone", tmp / "nowhere" / "python", bin_ / "sync_all.py")
    _agent(agents, "com.m3memory.mystery", this_py, bin_ / "sync_all.py")
    _agent(agents, "com.example.notm3", "/usr/bin/python3", "/opt/other-app/bin/job.py")

    jobs = _by_ident(ji.inventory(str(ROOT)))

    assert "com.example.notm3" not in jobs, "a job that does not run m3 is not listed"
    assert jobs["com.m3memory.cognitiveloop"].verdict == "current"
    assert (
        jobs["com.m3memory.dashboard"].verdict == "outdated" and "2026.10.4.0" in jobs["com.m3memory.dashboard"].detail
    )
    assert jobs["com.m3memory.loopwatchdog"].verdict == "newer"
    assert jobs["com.m3memory.gone"].verdict == "dangling"
    assert jobs["com.m3memory.mystery"].verdict == "unknown"


def test_repair_removes_outdated_retired_and_dangling_and_backs_them_up(mac, monkeypatch):
    ji, agents, this_py, tmp, calls = mac
    old_py = _install(tmp / "old", "2026.10.4.0")
    new_py = _install(tmp / "new", "2027.1.0")
    bin_ = ROOT / "bin"
    _agent(agents, "com.m3memory.cognitiveloop", this_py.parent / "m3-cognitive-loop", bin_ / "m3_cognitive_loop.py")
    _agent(agents, "com.m3memory.dashboard", old_py, bin_ / "dashboard_server.py")
    _agent(agents, "com.m3memory.loopwatchdog", new_py, bin_ / "m3_loop_watchdog.py")
    _agent(agents, "com.m3memory.gone", tmp / "nowhere" / "python", bin_ / "sync_all.py")
    _agent(agents, "com.m3memory.oldthing", this_py, bin_ / "sync_all.py")
    _agent(agents, "com.m3memory.mystery", this_py, bin_ / "secret_rotator.py")
    monkeypatch.setitem(ji.RETIRED_JOBS, "com.m3memory.oldthing", "2026.10.15.0")

    results = {j.ident: (ok, msg) for j, ok, msg in ji.repair(str(ROOT))}

    assert set(results) == {"com.m3memory.dashboard", "com.m3memory.gone", "com.m3memory.oldthing"}
    assert all(ok for ok, _ in results.values())
    left = {p.name for p in agents.iterdir()}
    assert left == {"com.m3memory.cognitiveloop.plist", "com.m3memory.loopwatchdog.plist", "com.m3memory.mystery.plist"}
    backed_up = {p.name for p in (tmp / "backup").iterdir()}
    assert backed_up == {"com.m3memory.dashboard.plist", "com.m3memory.gone.plist", "com.m3memory.oldthing.plist"}
    assert any(c[:2] == ["launchctl", "bootout"] and c[2].endswith("/com.m3memory.dashboard") for c in calls)


def test_without_its_own_version_nothing_is_called_outdated(mac):
    """A development checkout cannot tell older from newer, so it removes nothing
    that merely runs another install."""
    ji, agents, _this, tmp, _calls = mac
    ji._this_version = lambda: None  # monkeypatch undoes the fixture's patch either way
    old_py = _install(tmp / "old", "2026.10.4.0")
    _agent(agents, "com.m3memory.dashboard", old_py, ROOT / "bin" / "dashboard_server.py")
    assert _by_ident(ji.inventory(str(ROOT)))["com.m3memory.dashboard"].verdict == "unknown"


def test_linux_units_are_judged_the_same_way(tmp_path, monkeypatch):
    import install_schedules as sched
    from doctor import job_inventory as ji

    units = tmp_path / "systemd"
    units.mkdir()
    this_py = _install(tmp_path / "this", "2026.10.15.0")
    old_py = _install(tmp_path / "old", "2026.10.4.0")
    monkeypatch.setattr(ji, "_SYSTEMD_USER", str(units))
    monkeypatch.setattr(ji, "is_windows", lambda: False)
    monkeypatch.setattr(ji, "is_macos", lambda: False)
    monkeypatch.setattr(ji, "_cron_jobs", list)
    monkeypatch.setattr(ji, "_this_version", lambda: "2026.10.15.0")
    monkeypatch.setattr(ji, "_taken_over_by_loop", set)
    monkeypatch.setattr(sched, "_venv_python", lambda root, windowless=False: str(this_py))
    (units / "m3-cognitive-loop.service").write_text(
        f"[Service]\nExecStart={this_py} {ROOT / 'bin' / 'm3_cognitive_loop.py'} --interval 300\n", encoding="utf-8"
    )
    (units / "m3-dashboard.service").write_text(
        f"[Service]\nExecStart={old_py} {ROOT / 'bin' / 'dashboard_server.py'}\n", encoding="utf-8"
    )
    (units / "nginx.service").write_text("[Service]\nExecStart=/usr/sbin/nginx\n", encoding="utf-8")

    jobs = _by_ident(ji.inventory(str(ROOT)))
    assert set(jobs) == {"m3-cognitive-loop.service", "m3-dashboard.service"}
    assert jobs["m3-cognitive-loop.service"].verdict == "current"
    assert jobs["m3-dashboard.service"].verdict == "outdated"


def test_a_windows_task_of_an_older_install_is_deleted_after_its_xml_is_saved(tmp_path, monkeypatch):
    import install_schedules as sched
    from doctor import job_inventory as ji
    from doctor import schedule_probe as sp

    this_py = _install(tmp_path / "this", "2026.10.15.0")
    old_py = _install(tmp_path / "old", "2026.10.4.0")
    monkeypatch.setattr(ji, "is_windows", lambda: True)
    monkeypatch.setattr(ji, "_task_names", lambda: {"AgentOS_HourlySync"})
    monkeypatch.setattr(ji, "_this_version", lambda: "2026.10.15.0")
    monkeypatch.setattr(ji, "_taken_over_by_loop", set)
    monkeypatch.setattr(ji, "_backup_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sched, "_venv_python", lambda root, windowless=False: str(this_py))
    monkeypatch.setattr(sp, "_query_task_xml", lambda name: "<xml/>")
    monkeypatch.setattr(sp, "_parse_exec_paths", lambda xml: (str(old_py), str(ROOT / "bin" / "sync_all.py")))
    calls = []

    class _R:
        returncode, stdout, stderr = 0, "<Task/>", ""

    monkeypatch.setattr(ji, "_run", lambda argv, **k: calls.append(argv) or _R())

    (job, ok, _msg) = ji.repair(str(ROOT))[0]
    assert job.verdict == "outdated" and ok
    assert (tmp_path / "AgentOS_HourlySync.xml").read_text(encoding="utf-8") == "<Task/>"
    assert ["schtasks", "/Delete", "/TN", "AgentOS_HourlySync", "/F"] in calls


@pytest.mark.requires_posix  # symlinks need privileges on Windows
def test_a_venv_python_that_links_outside_the_venv_still_belongs_to_it(tmp_path):
    """A venv's bin/python links to the base interpreter; following the link lost
    the environment and called m3's own jobs unknown (measured on a pipx install)."""
    from doctor import job_inventory as ji

    base = tmp_path / "base" / "python3.14"
    base.parent.mkdir()
    base.write_text("", encoding="utf-8")
    env = tmp_path / "venv"
    (env / "bin").mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (env / "bin" / "python").symlink_to(base)
    assert ji._env_root(str(env / "bin" / "python")) == str(env)


def test_every_launchd_template_renders_to_valid_xml(tmp_path):
    """launchd accepted the waiter template with a "--" inside a comment, which
    strict XML parsers reject; the audit then could not read the waiter at all."""
    import install_schedules as sched

    (tmp_path / "python").write_text("", encoding="utf-8")
    for tpl in sorted((ROOT / "bin").glob("*.plist")):
        rendered = sched._render_template(str(tpl), str(ROOT), str(tmp_path / "python"))
        plistlib.loads(rendered.encode("utf-8"))


def test_an_unreadable_m3_plist_is_listed_not_skipped(mac):
    ji, agents, _this, _tmp, _calls = mac
    (agents / "com.m3memory.broken.plist").write_text("<plist><dict><key>Label", encoding="utf-8")
    job = {j.ident: j for j in ji.inventory(str(ROOT))}["com.m3memory.broken"]
    assert job.verdict == "unknown" and job.detail.startswith("unreadable")
