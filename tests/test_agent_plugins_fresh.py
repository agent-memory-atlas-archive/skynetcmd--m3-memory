"""The Codex plugin skills and GEMINI.md are generated from commands/ and
skills/m3-guide/ by bin/gen_agent_plugins.py; a source edit without a re-run
fails here. The check itself is shown able to fail (a stale, a missing and an
orphaned output in a planted tree) and the rewrites able to refuse a source that
no longer carries the text they rewrite."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "gen_agent_plugins.py"


def test_generated_agent_plugin_files_are_fresh():
    r = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, "run `python bin/gen_agent_plugins.py`:\n" + r.stdout + r.stderr


@pytest.fixture
def gen(tmp_path, monkeypatch):
    """The generator pointed at a copy of its sources and outputs."""
    spec = importlib.util.spec_from_file_location("gen_agent_plugins_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for rel in ("commands", "skills/m3-guide", "plugins/codex/skills"):
        shutil.copytree(ROOT / rel, tmp_path / rel)
    shutil.copy2(ROOT / "GEMINI.md", tmp_path / "GEMINI.md")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "COMMANDS_DIR", tmp_path / "commands")
    monkeypatch.setattr(mod, "GUIDE", tmp_path / "skills" / "m3-guide" / "SKILL.md")
    monkeypatch.setattr(mod, "CODEX_SKILLS", tmp_path / "plugins" / "codex" / "skills")
    monkeypatch.setattr(mod, "GEMINI_MD", tmp_path / "GEMINI.md")
    return mod, tmp_path


def test_check_catches_stale_missing_and_orphaned_outputs(gen, capsys):
    mod, root = gen
    assert mod.main(["--check"]) == 0
    search = root / "commands" / "search.md"
    search.write_text(search.read_text(encoding="utf-8") + "\nOne more line.\n", encoding="utf-8")
    (root / "commands" / "brand-new.md").write_text("---\nname: brand-new\ndescription: d\n---\n\nbody\n", "utf-8")
    (root / "plugins" / "codex" / "skills" / "m3-retired").mkdir()
    capsys.readouterr()
    assert mod.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "m3-search/SKILL.md: stale" in out
    assert "m3-brand-new/SKILL.md: missing" in out
    assert "m3-retired: no source produces it" in out
    assert mod.main([]) == 0
    assert not (root / "plugins" / "codex" / "skills" / "m3-retired").exists()
    assert mod.main(["--check"]) == 0


def test_a_crlf_checkout_is_not_stale(gen):
    mod, root = gen
    p = root / "GEMINI.md"
    p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert mod.main(["--check"]) == 0


def test_a_rewrite_that_matches_nothing_fails_the_run(gen, capsys):
    mod, root = gen
    notify = root / "commands" / "notify.md"
    notify.write_text(notify.read_text(encoding="utf-8").replace("Claude Code polls", "The client polls"), "utf-8")
    assert mod.main(["--check"]) == 1
    assert "match no source" in capsys.readouterr().err


def test_codex_skills_carry_no_claude_command_syntax():
    for skill in (ROOT / "plugins" / "codex" / "skills").glob("m3-*/SKILL.md"):
        if skill.parent.name == "m3-guide":
            continue
        text = skill.read_text(encoding="utf-8")
        assert "$ARGUMENTS" not in text and "/m3:" not in text and "`m3:" not in text, skill
        assert text.startswith(f"---\nname: {skill.parent.name}\ndescription: "), skill
