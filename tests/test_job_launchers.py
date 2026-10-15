"""m3's background jobs run through launchers named for them (m3_memory.jobs), so
a login item or a process reads "m3-cognitive-loop" rather than "python"."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from m3_memory import jobs

ROOT = Path(__file__).resolve().parents[1]


def test_every_launcher_is_declared_and_every_job_script_exists():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    declared = {name for name, target in scripts.items() if target == "m3_memory.jobs:run_script"}
    assert declared == set(jobs.JOB_LAUNCHERS.values())
    for script in jobs.JOB_LAUNCHERS:
        assert (ROOT / "bin" / script).is_file(), script


def test_no_job_template_is_left_running_as_python():
    """A new service template whose script has no launcher would show as "python" again."""
    for tpl in [*ROOT.glob("bin/*.plist"), *ROOT.glob("bin/*.service")]:
        text = tpl.read_text(encoding="utf-8")
        if "[M3_PYTHON]" not in text:
            continue
        found = re.findall(r"\[M3_MEMORY_ROOT\]/bin/([A-Za-z0-9_]+\.py)", text)
        assert found and found[0] in jobs.JOB_LAUNCHERS, f"{tpl.name}: {found}"


def test_every_periodic_job_has_a_launcher():
    import install_schedules

    for spec in install_schedules.unix_periodic_specs(str(ROOT)):
        assert os.path.basename(spec["args"][0]) in jobs.JOB_LAUNCHERS, spec["name"]


def test_the_launcher_is_used_when_this_install_has_it(tmp_path):
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    assert jobs.launcher_for(str(python), "/x/bin/sync_all.py") == str(python), "older install: python"
    (tmp_path / "m3-sync").write_text("", encoding="utf-8")
    assert jobs.launcher_for(str(python), "/x/bin/sync_all.py") == str(tmp_path / "m3-sync")
    assert jobs.launcher_for(str(python), "/x/bin/not_a_job.py") == str(python)


def test_rendered_job_definitions_name_the_launcher(tmp_path, monkeypatch):
    import install_schedules

    (tmp_path / "python").write_text("", encoding="utf-8")
    (tmp_path / "m3-cognitive-loop").write_text("", encoding="utf-8")
    (tmp_path / "m3-sync").write_text("", encoding="utf-8")
    python = str(tmp_path / "python")
    rendered = install_schedules._render_template(
        str(ROOT / "bin" / "com.m3memory.cognitiveloop.plist"), str(ROOT), python
    )
    assert f"<string>{tmp_path / 'm3-cognitive-loop'}</string>" in rendered
    spec = next(s for s in install_schedules.unix_periodic_specs(str(ROOT)) if s["name"] == "AgentOS_HourlySync")
    assert install_schedules._launchd_plist(spec, python)["ProgramArguments"][0] == str(tmp_path / "m3-sync")


def test_a_launcher_runs_the_script_as_python_would(tmp_path):
    (tmp_path / "memory_bridge.py").write_text("", encoding="utf-8")  # what marks a bin/ dir
    script = tmp_path / "job.py"
    script.write_text(
        "import sys\nprint(sys.argv[1:], sys.path[0] == __import__('os').path.dirname(__file__))\n"
        "if __name__ == '__main__':\n    sys.exit(3)\n",
        encoding="utf-8",
    )
    code = "import sys; from m3_memory.jobs import run_script; sys.argv[0] = 'm3-x'; run_script()"
    r = subprocess.run(
        [sys.executable, "-c", code, "-u", str(script), "--flag", "v"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
        env={**os.environ, "M3_PATH_BIN": str(tmp_path)},
    )
    assert r.returncode == 3, r.stderr
    assert r.stdout.strip() == "['--flag', 'v'] True"


def test_a_launcher_without_a_script_says_how_to_use_it():
    with pytest.raises(SystemExit) as e:
        old = sys.argv
        sys.argv = ["m3-sync"]
        try:
            jobs.run_script()
        finally:
            sys.argv = old
    assert "usage: m3-sync" in str(e.value)
