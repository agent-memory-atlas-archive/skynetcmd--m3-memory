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


class _Ok:
    returncode, stdout, stderr = 0, "", ""


class _Fail:
    returncode, stdout, stderr = 5, "", "Boot-out failed: 5: Input/output error"


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
    monkeypatch.setattr(ji, "_run", lambda argv, **k: calls.append(argv) or _Ok())
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


@pytest.mark.parametrize("still_loaded", [True, False])
def test_a_failed_bootout_keeps_the_plist_only_while_launchd_still_holds_the_job(mac, monkeypatch, still_loaded):
    ji, agents, _this, tmp, _calls = mac
    _agent(agents, "com.m3memory.gone", tmp / "nowhere" / "python", ROOT / "bin" / "sync_all.py")
    monkeypatch.setattr(
        ji,
        "_run",
        lambda argv, **k: _Fail() if argv[1] == "bootout" else (_Ok() if still_loaded else _Fail()),
    )
    ((_job, ok, msg),) = ji.repair(str(ROOT))
    assert (agents / "com.m3memory.gone.plist").exists() is still_loaded
    assert ok is not still_loaded
    if still_loaded:
        assert "exited 5" in msg and "launchctl bootout gui/" in msg


def test_a_failed_systemd_disable_keeps_the_unit_file(tmp_path, monkeypatch):
    from doctor import job_inventory as ji

    unit = tmp_path / "m3-dashboard.service"
    unit.write_text("[Service]\n", encoding="utf-8")
    (tmp_path / "backup").mkdir()
    calls = []
    monkeypatch.setattr(ji, "_run", lambda argv, **k: calls.append(argv) or _Fail())
    ok, msg = ji._remove(ji.Job("systemd", unit.name, None, None, str(unit)), str(tmp_path / "backup"))
    assert not ok and unit.exists()
    assert "exited 5" in msg and f"systemctl --user disable --now {unit.name}" in msg
    assert ["systemctl", "--user", "daemon-reload"] not in calls


def test_a_task_whose_query_fails_is_unknown_with_the_error(monkeypatch):
    from doctor import job_inventory as ji
    from doctor import schedule_probe as sp

    monkeypatch.setattr(ji, "_task_names", lambda: {"AgentOS_HourlySync"})

    def boom(name):
        raise OSError("access denied")

    monkeypatch.setattr(sp, "_query_task_xml", boom)
    monkeypatch.setattr(ji, "_taken_over_by_loop", set)
    monkeypatch.setattr(ji, "expected_idents", lambda root: set())
    monkeypatch.setattr(ji, "_this_version", lambda: None)
    ((job,),) = [ji._task_jobs()]
    assert "access denied" in job.unreadable
    assert ji.judge([job], str(ROOT))[0].verdict == "unknown"


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


_REBOOT = (
    "@reboot M3_EMBED_GGUF=/home/u/bge-m3.gguf M3_EMBED_SERVER_PORT=8082 nohup "
    "/home/u/.local/bin/m3-embed-server > /home/u/embed.log 2>&1 &"
)


def test_cron_lines_name_their_program_after_any_schedule_form():
    """An @reboot schedule is one field; five were skipped, so the embedder line
    read as some other program (measured on a 2026.6 install)."""
    from doctor import job_inventory as ji

    assert ji._cron_command(_REBOOT) == ("/home/u/.local/bin/m3-embed-server", None)
    assert ji._cron_command("*/5 * * * * /v/bin/python /p/bin/sync_all.py --x") == (
        "/v/bin/python",
        "/p/bin/sync_all.py",
    )
    assert ji._cron_command("@daily env A=1 nice /x/m3-embed-server")[0] == "/x/m3-embed-server"


@pytest.fixture
def crontab(tmp_path, monkeypatch):
    """A fake crontab behind ji._run; returns (ji, state, calls)."""
    from doctor import job_inventory as ji

    state = {"text": f"{_REBOOT}\n0 3 * * * /usr/bin/backup.sh\n"}
    calls = []

    class _R:
        def __init__(self, out=""):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    def run(argv, **k):
        calls.append(argv)
        if argv == ["crontab", "-l"]:
            return _R(state["text"])
        if argv == ["crontab", "-"]:
            state["text"] = k["input"]
            return _R()
        return _R()

    monkeypatch.setattr(ji, "_run", run)
    monkeypatch.setattr(ji, "is_windows", lambda: False)
    monkeypatch.setattr(ji, "is_macos", lambda: False)
    monkeypatch.setattr(ji, "_SYSTEMD_USER", str(tmp_path / "no-units"))
    monkeypatch.setattr(ji, "_backup_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ji, "_this_version", lambda: "2026.10.15.0")
    monkeypatch.setattr(ji, "_taken_over_by_loop", set)
    return ji, state, calls


def test_a_cron_embedder_is_replaced_only_where_m3_can_run_the_service(crontab, monkeypatch):
    ji, _state, _calls = crontab
    for managed in (True, False):
        monkeypatch.setattr(ji, "_service_manager_reachable", lambda managed=managed: managed)
        (job,) = [j for j in ji.inventory(str(ROOT)) if j.kind == "cron"]
        assert (job.verdict == "replaced") is managed, job.verdict
    assert ji.repair(str(ROOT)) == [], "an unreplaced cron line is the user's: never removed"


def test_retiring_removes_that_line_only_and_stops_what_it_started(crontab, monkeypatch, tmp_path):
    ji, state, _calls = crontab
    monkeypatch.setattr(ji, "_service_manager_reachable", lambda: True)
    stopped = []
    monkeypatch.setattr(ji, "_stop_processes_of", lambda program: stopped.append(program) or [2157])
    (result,) = ji.retire_replaced_embedder(str(ROOT))
    assert result[1] and "stopped pid 2157" in result[2]
    assert state["text"] == "0 3 * * * /usr/bin/backup.sh\n", "the user's own line stays"
    assert stopped == ["/home/u/.local/bin/m3-embed-server"]
    assert _REBOOT in (tmp_path / "crontab.txt").read_text(encoding="utf-8")
    assert ji.retire_replaced_embedder(str(ROOT)) == [], "a second run finds nothing"


def test_only_this_users_processes_of_that_program_are_stopped(monkeypatch, tmp_path):
    import m3_core.seams as seams
    import psutil
    from doctor import job_inventory as ji

    prog = tmp_path / "m3-embed-server"
    prog.write_text("", encoding="utf-8")
    other = tmp_path / "other-server"
    other.write_text("", encoding="utf-8")

    class _P:
        def __init__(self, pid, exe, mine):
            self.info, self.mine, self.terminated = {"pid": pid, "exe": str(exe)}, mine, False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    procs = [_P(1, prog, True), _P(2, prog, False), _P(3, other, True)]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(seams, "owned_by_current_user", lambda p: p.mine)
    assert ji._stop_processes_of(str(prog)) == [1]
    assert [p.terminated for p in procs] == [True, False, False]
