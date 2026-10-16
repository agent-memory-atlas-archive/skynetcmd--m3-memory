"""deep_sync stages only the named paths, and a failed step exits non-zero.

It used to `git add .` the whole payload, push to `origin main` (the PUBLIC repo
in an m3-memory checkout), report a failed commit as "No changes", and exit 0.
Hermetic: a temp repo with a local bare remote.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import skip_reason

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason=skip_reason("dep", "git not on PATH"))


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    _git(work, "config", "user.email", "t@example.invalid")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "commit.gpgsign", "false")
    (work / "seed").write_text("x", encoding="utf-8")
    _git(work, "add", "seed")
    _git(work, "commit", "-q", "-m", "seed")
    _git(work, "remote", "add", "private", str(remote))
    ds = importlib.import_module("deep_sync")
    monkeypatch.setattr(ds, "WORKSPACE", str(work))
    return ds, work, remote


def test_only_the_named_paths_are_committed_and_pushed(repo):
    ds, work, remote = repo
    (work / "report.md").write_text("r", encoding="utf-8")
    (work / "bench_secret.json").write_text("never", encoding="utf-8")
    assert ds.main(["--remote", "private", "--branch", "main", "report.md"]) == 0
    pushed = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "--name-only", "main"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert "report.md" in pushed
    assert "bench_secret.json" not in pushed, "observed: an unnamed file was pushed"


def test_nothing_changed_is_reported_and_exits_clean(repo, caplog):
    import logging

    caplog.set_level(logging.INFO)
    ds, _work, _remote = repo
    assert ds.main(["--remote", "private", "--branch", "main", "seed"]) == 0
    assert "No changes" in caplog.text


def test_a_failed_push_exits_non_zero_with_git_stderr(repo, caplog):
    ds, work, _remote = repo
    (work / "report.md").write_text("r", encoding="utf-8")
    assert ds.main(["--remote", "no-such-remote", "--branch", "main", "report.md"]) == 1
    assert "FAILED at `git push no-such-remote" in caplog.text


def test_remote_and_branch_have_no_defaults(repo):
    ds, _work, _remote = repo
    with pytest.raises(SystemExit):
        ds.main(["report.md"])
