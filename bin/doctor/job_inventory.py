"""Every m3 background job on this machine, each with a verdict.

Login items, systemd units and scheduled tasks show a job's name, never whether it
is still wanted or which m3 it belongs to. Two "m3-cognitive-loop" entries, or an
entry a newer m3 no longer uses, were indistinguishable from the real ones. This
lists each m3 job with what it runs and a verdict:

  current   this version installs it, and it runs this install
  outdated  it runs another, OLDER m3 install (that install's version is shown)
  newer     it runs another, NEWER m3 install: reported, never removed
  retired   this version no longer uses it
  dangling  the program or script it runs no longer exists
  unknown   it looks like m3 but is none of the above: reported, never removed

``repair()`` (m3 setup, m3 doctor --fix) removes outdated, retired and dangling
launchd agents, systemd units and scheduled tasks, after copying each definition
to a backup. Tasks the cognitive loop has taken over are reported here and removed
by setup's governor step (governor_migration), not twice. Cron lines are reported
only: setup rewrites m3's own managed block.
"""

from __future__ import annotations

import glob
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from m3_core.seams import is_macos, is_windows

_LAUNCH_AGENTS = "~/Library/LaunchAgents"
_SYSTEMD_USER = "~/.config/systemd/user"
_M3_LABEL_PREFIXES = ("com.m3memory.", "com.skynetcmd.m3")
_REMOVABLE = ("outdated", "retired", "dangling", "replaced")
_EMBED_SERVER = "m3-embed-server"


def _run(argv: list[str], **kw):
    """subprocess.run, through one name the tests can replace."""
    return subprocess.run(argv, **kw)  # nosec B603 -- fixed argv, no shell


# Jobs a version stopped installing: {identifier: version that retired it}. Add an
# entry whenever a job is dropped, so installs that still carry it remove it.
RETIRED_JOBS: dict[str, str] = {}


@dataclass(slots=True)
class Job:
    kind: str  # launchd | systemd | cron | task
    ident: str  # label, unit name, task name, or the cron line
    program: str | None
    script: str | None
    definition: str | None  # the file that defines it (None for cron and tasks)
    verdict: str = "unknown"
    detail: str = ""
    unreadable: str = ""  # why the definition could not be read; judged unknown


# ── what counts as m3 ─────────────────────────────────────────────────────────


def _known_scripts() -> set[str]:
    import governor_migration as gm

    from m3_memory.jobs import JOB_LAUNCHERS

    names = set(JOB_LAUNCHERS)
    for markers in gm._UNIX_CRON_MARKERS.values():
        names.update(m for m in markers if m.endswith(".py"))
    names.update(gm._WINDOWS_ACTION_MARKERS.values())
    return names


def _launcher_names() -> set[str]:
    from m3_memory.jobs import JOB_LAUNCHERS

    return set(JOB_LAUNCHERS.values()) | {"m3-embed-server"}


def _is_m3(program: str | None, script: str | None) -> bool:
    return bool(
        (program and os.path.basename(program) in _launcher_names())
        or (script and os.path.basename(script) in _known_scripts())
    )


# ── discovery ─────────────────────────────────────────────────────────────────


def _launchd_jobs() -> list[Job]:
    jobs: list[Job] = []
    for path in sorted(glob.glob(os.path.join(os.path.expanduser(_LAUNCH_AGENTS), "*.plist"))):
        try:
            data = _read_plist(path)
        except Exception as e:  # noqa: BLE001 -- judged below by its name alone
            if os.path.basename(path).startswith(_M3_LABEL_PREFIXES):
                label = os.path.basename(path)[: -len(".plist")]
                jobs.append(Job("launchd", label, None, None, path, unreadable=f"unreadable: {e}"))
            continue
        label = str(data.get("Label") or os.path.basename(path)[: -len(".plist")])
        args = [str(a) for a in data.get("ProgramArguments") or ([data["Program"]] if data.get("Program") else [])]
        program = args[0] if args else None
        script = next((a for a in args[1:] if a.endswith(".py")), None)
        if label.startswith(_M3_LABEL_PREFIXES) or _is_m3(program, script):
            jobs.append(Job("launchd", label, program, script, path))
    return jobs


def _read_plist(path: str) -> dict:
    """A launchd plist. launchd accepts files a strict XML parser rejects (a "--"
    inside a comment, as m3's own waiter template once had), so Apple's plutil is
    asked when plistlib refuses."""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        data = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 -- try the parser launchd itself uses
        r = _run(["plutil", "-convert", "xml1", "-o", "-", path], capture_output=True, timeout=20)
        if getattr(r, "returncode", 1) != 0:
            raise
        data = plistlib.loads(r.stdout)
    return data if isinstance(data, dict) else {}


_EXECSTART = re.compile(r"^\s*ExecStart\s*=\s*(.+?)\s*$", re.MULTILINE)
_TIMER_UNIT = re.compile(r"^\s*Unit\s*=\s*(.+?)\s*$", re.MULTILINE)


def _words(command: str) -> list[str]:
    """A command line's words: install_schedules' reading, the one owner."""
    import install_schedules as sched

    return sched._unit_words(command)


def _systemd_jobs() -> list[Job]:
    jobs: list[Job] = []
    base = os.path.expanduser(_SYSTEMD_USER)
    for path in sorted(glob.glob(os.path.join(base, "*.service")) + glob.glob(os.path.join(base, "*.timer"))):
        name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        if name.endswith(".timer"):
            # A timer runs no program itself: it starts a service (Unit=, by
            # default the same-named .service), and is judged by what that runs.
            # Without this, m3's own watchdog timer read as "not a job this
            # version installs".
            u = _TIMER_UNIT.search(text)
            service = u.group(1).strip() if u else name[: -len(".timer")] + ".service"
            try:
                with open(os.path.join(base, service), encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                text = ""
        m = _EXECSTART.search(text)
        toks = _words(m.group(1).lstrip("-@+!:")) if m else []
        program = toks[0] if toks else None
        script = next((t for t in toks[1:] if t.endswith(".py")), None)
        if name.startswith("m3-") or _is_m3(program, script):
            jobs.append(Job("systemd", name, program, script, path))
    return jobs


def _cron_jobs() -> list[Job]:
    try:
        r = _run(["crontab", "-l"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    jobs: list[Job] = []
    for line in (r.stdout or "").splitlines() if r.returncode == 0 else []:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        program, script = _cron_command(line)
        if _is_m3(program, script):
            jobs.append(Job("cron", line.strip(), program, script, None))
    return jobs


_CRON_WRAPPERS = {"nohup", "env", "nice", "ionice", "setsid", "exec"}


def _cron_command(line: str) -> tuple[str | None, str | None]:
    """(program, script) of a crontab line. An @reboot-style schedule is one field,
    not five; leading VAR=value assignments and wrappers (nohup, env) are not the
    program. Reading five fields made an @reboot embedder line unrecognisable."""
    toks = _words(line)
    toks = toks[1:] if toks and toks[0].startswith("@") else toks[5:]
    while toks and (toks[0] in _CRON_WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0])):
        toks = toks[1:]
    if not toks:
        return None, None
    return toks[0], next((t for t in toks[1:] if t.endswith(".py")), None)


def _service_manager_reachable() -> bool:
    """Whether m3 can run its embedder as a service here: launchd on macOS, a
    systemd user session on Linux. Without one, a cron line is the only autostart
    (setup itself suggests it), so it is not replaced."""
    if is_macos():
        return True
    from m3_core.seams import is_linux

    if not is_linux():
        return False
    from m3_core.seams import user_service_runtime_dir

    # And reachable from THIS process: retiring the old server where systemctl
    # --user cannot start the new one would leave no embedder at all.
    runtime = user_service_runtime_dir()
    return runtime is not None and os.environ.get("XDG_RUNTIME_DIR") == runtime


def _task_names() -> set[str]:
    import governor_migration as gm

    names: set[str] = set()
    try:
        r = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=60)
        for line in (r.stdout or "").splitlines():
            leaf = line.split(",", 1)[0].strip('"').rsplit("\\", 1)[-1]
            if leaf.startswith("AgentOS_"):
                names.add(leaf)
    except (OSError, subprocess.SubprocessError):
        pass
    return names | gm.detect_windows_legacy_action_tasks()


def _task_jobs() -> list[Job]:
    from doctor import schedule_probe as sp

    jobs: list[Job] = []
    for name in sorted(_task_names()):
        try:
            xml = sp._query_task_xml(name)
            program, script = sp._parse_exec_paths(xml) if xml else (None, None)
        except Exception as e:  # noqa: BLE001 -- one unreadable task must not hide the others; judged unknown
            jobs.append(Job("task", name, None, None, None, unreadable=f"could not query: {type(e).__name__}: {e}"))
            continue
        jobs.append(Job("task", name, program, script, None))
    return jobs


def discover() -> list[Job]:
    """The m3 jobs registered with this OS's scheduler, before any verdict."""
    if is_windows():
        return _task_jobs()
    if is_macos():
        return _launchd_jobs() + _cron_jobs()
    return _systemd_jobs() + _cron_jobs()


# ── installs and versions ─────────────────────────────────────────────────────


def _env_root(path: str | None) -> str | None:
    """The Python environment a program lives in (the directory with pyvenv.cfg)."""
    if not path:
        return None
    # Not realpath: a venv's bin/python is a link to the base interpreter, and
    # following it leaves the venv (measured on a pipx install).
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(8):
        if os.path.exists(os.path.join(d, "pyvenv.cfg")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _install_version(env: str | None) -> str | None:
    if not env:
        return None
    pattern = os.path.join(env, "**", "site-packages", "m3_memory-*.dist-info")
    for dist in glob.glob(pattern, recursive=True):
        m = re.match(r"m3_memory-(.+)\.dist-info$", os.path.basename(dist))
        if m:
            return m.group(1)
    return None


def _version_key(v: str) -> tuple:
    try:
        from packaging.version import Version

        return (0, Version(v))
    except Exception:  # noqa: BLE001 -- unparseable: compare as text, never "older"
        return (1, v)


def _this_version() -> str | None:
    try:
        from m3_memory import __version__

        return str(__version__) or None
    except Exception:  # noqa: BLE001 -- unknown: nothing is judged older
        return None


# ── verdicts ──────────────────────────────────────────────────────────────────


def expected_idents(m3_memory_root: str) -> set[str]:
    """The job identifiers this version installs on this OS."""
    import install_schedules as sched

    bin_dir = os.path.join(m3_memory_root, "bin")
    if is_windows():
        return {t["name"] for t in sched.get_schedule_specs(m3_memory_root)}
    if is_macos():
        labels = set()
        for tpl in glob.glob(os.path.join(bin_dir, "*.plist")):
            with open(tpl, "rb") as f:
                text = f.read().decode("utf-8", "replace")
            m = re.search(r"<key>Label</key>\s*<string>([^<]+)</string>", text)
            if m:
                labels.add(m.group(1))
        labels |= {sched.launchd_label(s["name"]) for s in sched.unix_periodic_specs(m3_memory_root)}
        return labels | {sched._RUST_EMBED_LABEL}
    units = {os.path.basename(p) for p in glob.glob(os.path.join(bin_dir, "*.service"))}
    units |= {os.path.basename(p) for p in glob.glob(os.path.join(bin_dir, "*.timer"))}
    return units | {sched._LINUX_EMBED_UNIT}


def _taken_over_by_loop() -> set[str]:
    """Scheduled tasks the cognitive loop now runs (governor_migration decides)."""
    try:
        import governor_migration as gm

        if gm.cognitive_loop_installed():
            return set(gm.detect_scheduled_tasks().get("eligible") or [])
    except Exception:  # noqa: BLE001 -- unknown: claim nothing
        pass
    return set()


def judge(jobs: list[Job], m3_memory_root: str) -> list[Job]:
    import install_schedules as sched

    expected = expected_idents(m3_memory_root)
    taken_over = _taken_over_by_loop()
    this_env = _env_root(sched._venv_python(m3_memory_root))
    this_bin = os.path.realpath(os.path.join(m3_memory_root, "bin"))
    mine = _this_version()
    managed = None  # asked once, only when a cron embedder line is found
    for j in jobs:
        if j.unreadable:
            j.verdict, j.detail = "unknown", j.unreadable
            continue
        if j.kind == "cron" and j.program and os.path.basename(j.program) == _EMBED_SERVER:
            managed = _service_manager_reachable() if managed is None else managed
            if managed:
                j.verdict = "replaced"
                j.detail = "m3 runs its embedder as a service now; this line starts an older one at boot"
                continue
        env = _env_root(j.program)
        # The program's environment decides; the script's place only when the
        # program is not in an environment (a checkout run by the system python).
        if env is not None:
            ours = env == this_env
        else:
            ours = j.script is not None and os.path.dirname(os.path.realpath(j.script)) == this_bin
        missing = [p for p in (j.program, j.script) if p and not os.path.exists(p)]
        if missing:
            j.verdict, j.detail = "dangling", "missing: " + ", ".join(missing)
        elif j.ident in RETIRED_JOBS:
            j.verdict, j.detail = "retired", f"not used since {RETIRED_JOBS[j.ident]}"
        elif j.ident in taken_over:
            j.verdict, j.detail = "retired", "the cognitive loop runs this now; m3 setup removes it"
        elif not ours and env:
            theirs = _install_version(env)
            if theirs and mine and _version_key(theirs) < _version_key(mine):
                j.verdict, j.detail = "outdated", f"runs m3 {theirs} at {env}"
            elif theirs and mine and _version_key(theirs) > _version_key(mine):
                j.verdict, j.detail = "newer", f"runs m3 {theirs} at {env}"
            else:
                j.verdict, j.detail = "unknown", f"runs another m3 install at {env} (version {theirs or 'unknown'})"
        elif ours and (j.ident in expected or j.kind == "cron"):
            j.verdict, j.detail = "current", ""
        else:
            j.verdict, j.detail = "unknown", "not a job this version installs"
    return jobs


def inventory(m3_memory_root: str) -> list[Job]:
    return judge(discover(), m3_memory_root)


# ── repair ────────────────────────────────────────────────────────────────────


def _backup_dir() -> str:
    from m3_core.paths import get_m3_config_root

    d = os.path.join(get_m3_config_root(), "removed-jobs", datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    os.makedirs(d, exist_ok=True)
    return d


def _remove(j: Job, backup: str) -> tuple[bool, str]:
    if j.kind == "launchd" and j.definition:
        shutil.copy2(j.definition, backup)
        uid = getattr(os, "getuid", lambda: 0)()  # launchd exists only where getuid does
        target = f"gui/{uid}/{j.ident}"
        r = _run(["launchctl", "bootout", target], capture_output=True, text=True, timeout=30)
        # bootout fails for a job that was never loaded too; only a job launchd
        # still holds blocks removal (deleting its plist would orphan it).
        if r.returncode != 0 and _run(["launchctl", "print", target], capture_output=True, timeout=30).returncode == 0:
            return (
                False,
                f"launchctl bootout {target} exited {r.returncode} ({(r.stderr or r.stdout or '').strip()}); "
                f"still loaded, so {j.definition} was kept. Retry: launchctl bootout {target}",
            )
        os.remove(j.definition)
        return True, f"unloaded and removed {j.definition}"
    if j.kind == "systemd" and j.definition:
        shutil.copy2(j.definition, backup)
        r = _run(["systemctl", "--user", "disable", "--now", j.ident], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            # Hazard: removing the unit file of a running/enabled unit leaves it
            # running with no definition. Keep it; report the exact retry.
            return (
                False,
                f"systemctl --user disable --now {j.ident} exited {r.returncode} "
                f"({(r.stderr or r.stdout or '').strip()}); {j.definition} was kept. "
                f"Retry: systemctl --user disable --now {j.ident}",
            )
        os.remove(j.definition)
        r = _run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return (
                True,
                f"disabled and removed {j.definition}; systemctl --user daemon-reload exited {r.returncode} "
                f"({(r.stderr or r.stdout or '').strip()}): run it by hand",
            )
        return True, f"disabled and removed {j.definition}"
    if j.kind == "task":
        xml = _run(["schtasks", "/Query", "/TN", j.ident, "/XML"], capture_output=True, text=True, timeout=30)
        if xml.returncode == 0:
            with open(os.path.join(backup, f"{j.ident}.xml"), "w", encoding="utf-8") as f:
                f.write(xml.stdout)
        r = _run(["schtasks", "/Delete", "/TN", j.ident, "/F"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, f"deleted task {j.ident}"
        return (
            False,
            f'could not delete task {j.ident} ({(r.stderr or r.stdout).strip()}); as administrator: schtasks /Delete /TN "{j.ident}" /F',
        )
    if j.kind == "cron":
        return _remove_cron_line(j, backup)
    return False, "reported only: remove it by hand"


def _remove_cron_line(j: Job, backup: str) -> tuple[bool, str]:
    """Drop this exact line from the user's crontab (the whole crontab is saved
    first), then stop the process it started: this user's, running that program."""
    cur = _run(["crontab", "-l"], capture_output=True, text=True, timeout=20)
    if cur.returncode != 0:
        return False, f"could not read the crontab ({(cur.stderr or '').strip()})"
    with open(os.path.join(backup, "crontab.txt"), "w", encoding="utf-8") as f:
        f.write(cur.stdout)
    kept = [ln for ln in cur.stdout.splitlines() if ln.strip() != j.ident]
    w = _run(
        ["crontab", "-"], input="\n".join(kept) + ("\n" if kept else ""), capture_output=True, text=True, timeout=20
    )
    if w.returncode != 0:
        return False, f"could not rewrite the crontab ({(w.stderr or '').strip()})"
    stopped = _stop_processes_of(j.program)
    return True, "removed the crontab line" + (f" and stopped pid {', '.join(map(str, stopped))}" if stopped else "")


def _stop_processes_of(program: str | None) -> list[int]:
    """Stop this user's processes whose executable is ``program``; their pids."""
    if not program:
        return []
    try:
        import psutil
        from m3_core.seams import owned_by_current_user
    except ImportError:
        return []
    target = os.path.realpath(program)
    stopped = []
    for p in psutil.process_iter(["pid", "exe"]):
        try:
            if p.info.get("exe") and os.path.realpath(p.info["exe"]) == target and owned_by_current_user(p) is True:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except psutil.TimeoutExpired:
                    p.kill()
                stopped.append(p.info["pid"])
        except (psutil.Error, OSError):
            continue
    return stopped


def retire_replaced_embedder(m3_memory_root: str | None = None) -> list[tuple[Job, bool, str]]:
    """Remove cron lines that start an older embedder, and stop what they started,
    BEFORE setup installs the embedder service: the old server held its port and
    the new service could not start. Never raises."""
    try:
        root = m3_memory_root or payload_root()
        out = []
        backup: str | None = None
        for j in inventory(root):
            if j.verdict == "replaced":
                backup = backup or _backup_dir()
                ok, msg = _remove(j, backup)
                out.append((j, ok, msg + (f" (backup in {backup})" if ok else "")))
        return out
    except Exception as e:  # noqa: BLE001 -- setup goes on with the old server running
        return [(Job("cron", "?", None, None, None), False, f"{type(e).__name__}: {e}")]


def repair(m3_memory_root: str, dry_run: bool = False) -> list[tuple[Job, bool, str]]:
    """Remove outdated, retired and dangling m3 jobs (backed up first). Tasks the
    cognitive loop took over are left to the governor step."""
    results: list[tuple[Job, bool, str]] = []
    backup: str | None = None
    for j in inventory(m3_memory_root):
        if j.verdict not in _REMOVABLE or "cognitive loop runs this now" in j.detail:
            continue
        if j.kind == "cron" and j.verdict != "replaced":
            continue  # a user's own crontab line is removed only when m3 replaced it
        if dry_run:
            results.append((j, True, "would remove"))
            continue
        backup = backup or _backup_dir()
        try:
            ok, msg = _remove(j, backup)
        except Exception as e:  # noqa: BLE001 -- one job must not stop the others
            ok, msg = False, f"{type(e).__name__}: {e}"
        results.append((j, ok, msg + (f" (backup in {backup})" if ok else "")))
    return results


def payload_root() -> str:
    """The m3 install this module belongs to (the directory holding bin/)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def repair_and_report(dry_run: bool = False) -> None:
    """repair(), one line per job (silent when nothing needs removing)."""
    try:
        results = repair(payload_root(), dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 -- never break setup or the doctor
        print(f"  [skipped] m3 job clean-up: {type(e).__name__}: {e}")
        return
    for j, ok, msg in results:
        print(f"  [{'fixed' if ok else 'not fixed'}] {j.verdict} job {j.ident}: {msg}")


# ── report ────────────────────────────────────────────────────────────────────


def run(brief: bool = False) -> int:
    """Print every m3 job with its verdict. Report-only: always returns 0."""
    root = payload_root()
    try:
        jobs = inventory(root)
    except Exception as e:  # noqa: BLE001 -- the probe must never crash the doctor
        print(
            f"jobs: could not list ({type(e).__name__}: {e})"
            if brief
            else f"\n=== m3 background jobs ===\n  could not list: {e}"
        )
        return 0
    attention = [j for j in jobs if j.verdict != "current"]
    if brief:
        print(
            f"jobs: {len(jobs)} m3 job(s), "
            + (f"{len(attention)} need attention: m3 doctor --verbose" if attention else "all current")
        )
        return 0
    print()
    print("=== m3 background jobs ===")
    if not jobs:
        print("  none registered")
        return 0
    width = max(len(j.ident) for j in jobs if j.kind != "cron") if any(j.kind != "cron" for j in jobs) else 20
    for j in jobs:
        name = j.ident if j.kind != "cron" else f"cron: {os.path.basename(j.script or j.program or '?')}"
        tail = f"  {j.detail}" if j.detail else ""
        print(f"  {j.verdict:<9} {name:<{width}}{tail}")
    if any(j.verdict in _REMOVABLE for j in jobs):
        print("  fix: m3 doctor --fix (each definition is backed up before it is removed)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
