"""Scheduled-task and service logs resolve to the logs root, never the payload.

Every log path used to be derived from the install root, so on a pipx install
the logs lived inside site-packages/m3_memory/logs — invisible, and deleted by
the next reinstall.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

from m3_core.paths import get_m3_logs_root  # noqa: E402

from conftest import write_sandbox_config  # noqa: E402


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _paths(tmp_path, **paths) -> None:
    """Rewrite the sandbox's m3_config.toml with these [paths] keys (plus the
    sandbox's memory_root when not given, so nothing resolves to the real home)."""
    write_sandbox_config("[paths]\n" + "".join(f'{k} = "{Path(v).as_posix()}"\n' for k, v in paths.items()))


def test_logs_root_precedence(monkeypatch, tmp_path):
    """[paths] logs_root > <memory_root>/logs > ~/.m3/logs; the env names are not read."""
    monkeypatch.setenv("M3_LOGS_ROOT", str(tmp_path / "env-ignored"))
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path / "env-ignored"))
    # The default is under the home: move the home into pytest's area to see it,
    # rather than resolving the developer's real ~/.m3.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (tmp_path / "config" / "m3_config.toml").write_text("schema_version = 1\n", encoding="utf-8")
    assert _norm(get_m3_logs_root()) == _norm(home / ".m3" / "logs")

    _paths(tmp_path, memory_root=tmp_path / "master")
    assert _norm(get_m3_logs_root()) == _norm(tmp_path / "master" / "logs")

    _paths(tmp_path, memory_root=tmp_path / "master", logs_root=tmp_path / "explicit")
    assert _norm(get_m3_logs_root()) == _norm(tmp_path / "explicit")


def test_schedule_specs_log_outside_the_install_root(monkeypatch, tmp_path):
    import install_schedules

    logs = tmp_path / "logs"  # the sandbox's [paths] logs_root
    payload = tmp_path / "payload"
    specs = install_schedules.get_schedule_specs(str(payload))

    log_args = [s["args"][s["args"].index("--log-file") + 1] for s in specs if "--log-file" in s["args"]]
    assert log_args, "precondition: some spec passes --log-file"
    for p in log_args:
        assert _norm(Path(p).parent) == _norm(logs), p
        assert not _norm(p).startswith(_norm(payload)), p


def test_service_templates_use_the_logs_placeholder():
    templates = [
        p for p in _BIN.iterdir() if p.suffix in (".plist", ".service", ".timer") or p.name == "crontab.template"
    ]
    assert templates, "precondition: templates found"
    offenders = [p.name for p in templates if "[M3_MEMORY_ROOT]/logs" in p.read_text(encoding="utf-8")]
    assert not offenders, f"templates still log under the payload: {offenders}"


def test_render_template_substitutes_the_logs_root(monkeypatch, tmp_path):
    import install_schedules

    monkeypatch.setenv("M3_LOGS_ROOT", str(tmp_path / "logs"))
    tpl = tmp_path / "t.service"
    tpl.write_text("ExecStart=[M3_PYTHON] x --log-file [M3_LOGS_ROOT]/a.log\n", encoding="utf-8")
    out = install_schedules._render_template(str(tpl), "/payload", "/py")
    assert "[M3_LOGS_ROOT]" not in out
    # get_m3_logs_root() normalises an env-supplied root to forward slashes.
    assert (tmp_path / "logs").as_posix() in out.replace("\\", "/"), out


def test_task_runtime_default_log_is_under_the_logs_root(monkeypatch, tmp_path):
    import _task_runtime

    monkeypatch.setenv("M3_LOGS_ROOT", str(tmp_path / "logs"))
    monkeypatch.delenv("M3_TASK_LOG_FILE", raising=False)
    resolved = _task_runtime._resolve_log_file(None)
    assert _norm(resolved.parent) == _norm(tmp_path / "logs"), resolved
