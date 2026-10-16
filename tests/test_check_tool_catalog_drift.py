"""The drift gate's git measurement must see new pages and fail when git fails.

`git diff` alone cannot see an untracked file, so a generator that CREATED a page
left the gate green; and an unread git exit code turned "git failed" into "no
drift". Both run against a throwaway repository, never the checkout.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "bin" / "check_tool_catalog_drift.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_tool_catalog_drift_t", _SRC)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs" / "tools").mkdir(parents=True)
    (tmp_path / "docs" / "tools" / "a.md").write_text("a\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_an_untracked_generated_page_is_drift(repo, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_ROOT", repo)
    assert mod._git_drift() == []
    (repo / "docs" / "tools" / "new_script.md").write_text("new\n")
    assert mod._git_drift() == ["docs/tools/new_script.md"]


def test_a_modified_generated_page_is_drift(repo, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_ROOT", repo)
    (repo / "docs" / "tools" / "a.md").write_text("changed\n")
    assert mod._git_drift() == ["docs/tools/a.md"]


def test_a_git_failure_is_not_zero_drift(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_ROOT", tmp_path)  # not a repository
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(mod.GitError):
        mod._git_drift()


def test_main_exits_2_when_drift_cannot_be_measured(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_ROOT", tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.setattr(mod, "_regenerate", lambda: True)
    assert mod.main([]) == 2


def test_the_tool_page_freshness_test_is_a_drift_test():
    mod = _load()
    assert "tests/test_generated_docs_fresh.py" in mod._DRIFT_TESTS
