"""The hooks folder (``~/.m3/hooks``): one launcher per hook, owned by m3.

A launcher must actually run its hook (planted: a hook that prints its argv); an
install writes only what changed and removes m3 launchers no longer listed, but
never a file that is the user's; the config line is the interpreter plus the
launcher; every listed hook script exists in the payload."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from m3_memory import hook_launchers as hl


@pytest.fixture
def folder(tmp_path, monkeypatch):
    d = tmp_path / "hooks"
    monkeypatch.setattr(hl, "hooks_dir", lambda: d)
    return d


def test_every_listed_hook_script_exists():
    from m3_memory._bin import bin_dir

    root = bin_dir()
    assert root is not None
    for name, hook in hl.HOOKS.items():
        assert os.path.isfile(os.path.join(root, *hook.script.split("/"))), name


def test_install_writes_each_launcher_once(folder):
    first = hl.install_launchers()
    assert len(first) == len(hl.HOOKS)
    assert all((folder / f"{n}.py").read_text(encoding="utf-8").startswith(hl.MARKER) for n in hl.HOOKS)
    assert hl.install_launchers() == [], "a current folder is left alone"


def test_a_retired_launcher_is_removed_and_a_user_file_is_kept(folder):
    folder.mkdir()
    (folder / "old-hook.py").write_text(hl.MARKER + ": old-hook\n", encoding="utf-8")
    (folder / "mine.py").write_text("print('the user wrote this')\n", encoding="utf-8")
    changes = hl.install_launchers()
    assert not (folder / "old-hook.py").exists()
    assert (folder / "mine.py").exists()
    assert any("removed" in c and "old-hook" in c for c in changes)


def test_a_launcher_runs_its_hook(folder, tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    (fake_bin / "hooks").mkdir(parents=True)
    (fake_bin / "hooks" / "echo.py").write_text("import sys, json\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    monkeypatch.setitem(hl.HOOKS, "echo-hook", hl.Hook("hooks/echo.py", ("--agent", "x")))
    text = hl.launcher_text("echo-hook").replace(
        "from m3_memory._bin import bin_dir", f"bin_dir = lambda: {str(fake_bin)!r}"
    )
    launcher = tmp_path / "echo-hook.py"
    launcher.write_text(text, encoding="utf-8")
    out = subprocess.run([sys.executable, str(launcher), "extra"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == '["--agent", "x", "extra"]'


def test_the_config_line_is_interpreter_plus_launcher(folder):
    line = hl.command_for("codex-capture", "C:\\Program Files\\py\\pythonw.exe")
    assert line.startswith('"C:/Program Files/py/pythonw.exe" ')
    assert line.endswith("codex-capture.py")
    assert hl.is_launcher_command(line)
    assert not hl.is_launcher_command("python /somewhere/else.py")
