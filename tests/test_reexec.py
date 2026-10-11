"""The OS seam's reexec, and the one owner that uses it (m3_core.paths.reexec_in_project_venv).

On Windows os.exec* is not an exec: measured 2026-10-11, the caller saw exit code 0
after 0.1 s for a child that exited 7, and an argument with a space was split.
The hourly sync judged pg_sync by that exit code."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "bin"))


def test_reexec_keeps_the_exit_code_and_arguments_with_spaces():
    child = "import sys; print(sys.argv[1]); sys.exit(7)"
    driver = f"import sys; from m3_memory._platform import reexec; reexec([sys.executable, '-c', {child!r}, 'a b'])"
    proc = subprocess.run([sys.executable, "-c", driver], cwd=_ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 7, f"exit code lost through reexec: {proc.returncode} ({proc.stderr.strip()})"
    assert proc.stdout.strip() == "a b", f"argument split through reexec: {proc.stdout!r}"


def test_no_reexec_from_inside_a_venv(monkeypatch, tmp_path):
    """A pip/pipx install runs in its own venv: a stray .venv beside the payload
    must not take over (the hourly-sync window flash)."""
    from m3_core import paths

    venv_python = tmp_path / "python"
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "resolve_venv_python", lambda: str(venv_python))
    monkeypatch.setattr(sys, "prefix", "/a/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    calls = []
    import m3_core.seams as seams

    monkeypatch.setattr(seams, "reexec", lambda argv: calls.append(argv))
    paths.reexec_in_project_venv()
    assert calls == []


def test_reexec_from_a_system_interpreter(monkeypatch, tmp_path):
    from m3_core import paths

    venv_python = tmp_path / "python"
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "resolve_venv_python", lambda: str(venv_python))
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(sys, "argv", ["pg_sync.py", "--db", "x"])
    calls = []
    import m3_core.seams as seams

    monkeypatch.setattr(seams, "reexec", lambda argv: calls.append(argv))
    paths.reexec_in_project_venv()
    assert calls == [[str(venv_python), "pg_sync.py", "--db", "x"]]
