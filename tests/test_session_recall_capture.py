"""Session recall part A: each captured turn records its project.

The OS seam's git_checkout reads the repository root and branch (no shell, no
window, bounded); chatlog_ingest computes the project ONCE per run from --cwd or
the transcript's own cwd and stores cwd / git_root / git_branch / tree on every
turn; the five capture hooks pass the envelope's cwd through; a Claude Code
compaction summary keeps its flag.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from conftest import create_full_chatlog_schema, isolate_chatlog_env, skip_reason

HOOK_DIR = os.path.join(os.path.dirname(__file__), "..", "bin", "hooks", "chatlog")
HOOKS = ("claude_code_precompact", "gemini_cli_onexit", "opencode_session_end", "openclaw_session_end", "codex_stop")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason=skip_reason("dep", "git is not installed"))


def _git_repo(path, branch="trunk"):
    """A real repository on an unborn branch (no commit needed)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
    return path


# ── the OS seam ──────────────────────────────────────────────────────────────


@needs_git
def test_git_checkout_reads_root_and_branch_from_a_subdirectory(tmp_path):
    from m3_memory._platform import git_checkout

    repo = _git_repo(tmp_path / "proj")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    got = git_checkout(str(sub))
    assert got is not None, "observed: None for a directory inside a git repository"
    assert os.path.normcase(got.root) == os.path.normcase(os.path.realpath(repo)), got
    assert got.branch == "trunk", f"observed branch {got.branch!r} on an unborn branch named trunk"


@needs_git
def test_git_checkout_names_a_detached_head_by_its_short_commit(tmp_path):
    from m3_memory._platform import git_checkout

    repo = _git_repo(tmp_path / "proj")
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-C",
            str(repo),
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
    got = git_checkout(str(repo))
    assert got is not None and got.branch == sha, f"observed {got!r}, expected branch {sha!r}"
    assert got.repo == got.root, f"a detached main checkout is its own repository; observed {got!r}"


def _commit(repo):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-C",
            str(repo),
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )


def _worktree(repo, path, *args):
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", *args, str(path)], check=True, capture_output=True)
    return path


@needs_git
def test_git_checkout_names_the_repository_of_a_linked_worktree(tmp_path):
    """A linked worktree (an agent's isolated copy) has its own top level, but
    its repository is the main checkout it was added from."""
    from m3_memory._platform import git_checkout

    repo = _git_repo(tmp_path / "m3-memory")
    _commit(repo)
    wt = _worktree(repo, tmp_path / "agent-wt", "-b", "feature")
    sub = wt / "bin"
    sub.mkdir()
    got = git_checkout(str(sub))
    assert got is not None
    assert os.path.normcase(got.root) == os.path.normcase(os.path.realpath(wt)), got
    assert os.path.normcase(got.repo) == os.path.normcase(os.path.realpath(repo)), got
    assert got.branch == "feature"

    main = git_checkout(str(repo))
    assert main is not None and main.repo == main.root, main


@needs_git
def test_git_checkout_names_the_repository_of_a_detached_worktree(tmp_path):
    from m3_memory._platform import git_checkout

    repo = _git_repo(tmp_path / "m3-memory")
    _commit(repo)
    wt = _worktree(repo, tmp_path / "detached-wt", "--detach")
    got = git_checkout(str(wt))
    assert got is not None and len(got.branch) >= 4, got
    assert os.path.normcase(got.repo) == os.path.normcase(os.path.realpath(repo)), got


def test_git_checkout_is_none_outside_a_repo_for_a_missing_dir_and_without_git(tmp_path, monkeypatch):
    from m3_memory import _platform

    plain = tmp_path / "plain"
    plain.mkdir()
    assert _platform.git_checkout(str(tmp_path / "missing")) is None
    assert _platform.git_checkout("") is None
    if shutil.which("git") is not None:
        # Only where tmp_path is outside every repository (it is on the lanes).
        probe = subprocess.run(["git", "-C", str(plain), "rev-parse", "--show-toplevel"], capture_output=True)
        if probe.returncode != 0:
            assert _platform.git_checkout(str(plain)) is None
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _platform.git_checkout(str(plain)) is None, "a missing git must read as no checkout, not raise"


def test_git_checkout_survives_a_hung_git(tmp_path, monkeypatch):
    from m3_memory import _platform

    def _hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))

    monkeypatch.setattr(shutil, "which", lambda name: "git")
    monkeypatch.setattr(subprocess, "run", _hang)
    assert _platform.git_checkout(str(tmp_path)) is None


def test_m3_core_seams_reexports_git_checkout():
    import m3_core.seams as seams

    from m3_memory import _platform

    assert seams.git_checkout is _platform.git_checkout


# ── project fields ───────────────────────────────────────────────────────────


@needs_git
def test_project_fields_inside_a_repo(tmp_path):
    import session_recall

    repo = _git_repo(tmp_path / "m3-memory", branch="main")
    (repo / "bin").mkdir()
    got = session_recall.project_fields(str(repo / "bin"))
    assert got["tree"] == "m3-memory" and got["git_branch"] == "main", got
    assert os.path.normcase(got["git_root"]) == os.path.normcase(os.path.realpath(repo)), got
    assert got["cwd"] == os.path.normpath(str(repo / "bin"))


@needs_git
def test_a_worktree_session_is_named_and_grouped_by_its_repository(tmp_path):
    import session_recall

    repo = _git_repo(tmp_path / "m3-memory", branch="main")
    _commit(repo)
    wt = _worktree(repo, tmp_path / "wcut-wt", "-b", "fix")
    in_wt = session_recall.project_fields(str(wt))
    in_main = session_recall.project_fields(str(repo))
    assert in_wt["tree"] == "m3-memory", f"named after the worktree's directory: {in_wt}"
    assert in_wt["git_branch"] == "fix"
    assert os.path.normcase(in_wt["git_root"]) == os.path.normcase(os.path.realpath(wt)), in_wt
    assert session_recall._project_key(in_wt) == session_recall._project_key(in_main), (in_wt, in_main)


def test_project_key_falls_back_to_the_root_for_turns_without_a_repo():
    import session_recall

    old = {"git_root": "/w/m3-memory", "cwd": "/w/m3-memory/bin"}
    assert session_recall._project_key(old) == os.path.normcase(os.path.normpath("/w/m3-memory"))


def test_project_fields_without_git_name_the_cwd(tmp_path, monkeypatch):
    import session_recall

    from m3_memory import _platform

    monkeypatch.setattr(_platform, "git_checkout", lambda cwd: None)
    import m3_core.seams as seams

    monkeypatch.setattr(seams, "git_checkout", lambda cwd: None)
    got = session_recall.project_fields(str(tmp_path / "homelab"))
    assert got == {"cwd": os.path.normpath(str(tmp_path / "homelab")), "tree": "homelab"}, got
    assert session_recall.project_fields("") == {}


# ── parsers and normalization ───────────────────────────────────────────────


def _claude_line(uuid, role, text, cwd, compact=False):
    rec = {
        "type": role,
        "uuid": uuid,
        "sessionId": "sess-cap-1",
        "timestamp": "2026-10-18T10:00:00.000Z",
        "cwd": cwd,
        "message": {"role": role, "content": text, "model": "claude-x" if role == "assistant" else ""},
    }
    if compact:
        rec["isCompactSummary"] = True
    return json.dumps(rec)


def test_claude_parser_keeps_cwd_and_the_compaction_flag():
    import chatlog_ingest

    raw = "\n".join(
        [
            _claude_line("u1", "user", "hello", "/w/proj"),
            _claude_line("u2", "user", "This session is being continued...", "/w/proj", compact=True),
        ]
    )
    items, sid = chatlog_ingest._parse_claude_code(raw)
    assert sid == "sess-cap-1"
    assert [it["cwd"] for it in items] == ["/w/proj", "/w/proj"]
    assert "metadata" not in items[0]
    assert items[1]["metadata"] == {"compact_summary": True}


def test_codex_parser_takes_cwd_from_session_meta():
    import chatlog_ingest

    raw = "\n".join(
        json.dumps(x)
        for x in (
            {"timestamp": "t0", "type": "session_meta", "payload": {"id": "cx-1", "cwd": "/w/codexproj"}},
            {
                "timestamp": "t1",
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
    )
    items, sid = chatlog_ingest._parse_codex(raw)
    assert sid == "cx-1" and items and items[0]["cwd"] == "/w/codexproj"


def test_normalize_stores_the_run_project_on_every_turn_and_drops_the_raw_cwd():
    import chatlog_ingest

    project = {"cwd": "/w/p", "git_root": "/w/p", "git_branch": "main", "tree": "p"}
    items = [
        {"content": "a", "role": "user", "cwd": "/elsewhere", "uuid": "1"},
        {"content": "b", "role": "user", "cwd": "/w/p", "uuid": "2", "metadata": {"compact_summary": True}},
    ]
    out = chatlog_ingest._normalize(items, "claude-code", "stop", "s1", project)
    assert all("cwd" not in it for it in out)
    assert out[0]["metadata"] == project
    assert out[1]["metadata"] == {**project, "compact_summary": True}
    bare = chatlog_ingest._normalize([{"content": "c", "role": "user", "uuid": "3"}], "claude-code", None, "s1", {})
    assert "metadata" not in bare[0], "a turn with no known project must carry no project fields"


def test_run_project_prefers_the_hook_cwd_and_reads_git_once(monkeypatch):
    import chatlog_ingest
    import session_recall

    calls = []
    monkeypatch.setattr(session_recall, "project_fields", lambda cwd: calls.append(cwd) or {"cwd": cwd, "tree": "x"})
    items = [{"cwd": "/old"}, {"cwd": "/newest"}]
    assert chatlog_ingest._run_project("/from-hook", items)["cwd"] == "/from-hook"
    assert chatlog_ingest._run_project("", items)["cwd"] == "/newest"
    assert chatlog_ingest._run_project("", [{}]) == {}
    assert calls == ["/from-hook", "/newest"], f"observed git reads {calls}: one per run, none without a cwd"


# ── end to end: a transcript lands with its project ─────────────────────────


@pytest.fixture
def ingest_env(tmp_path, monkeypatch):
    paths = isolate_chatlog_env(monkeypatch, tmp_path)
    import chatlog_ingest

    monkeypatch.setattr(chatlog_ingest, "_cursor_path", lambda: str(tmp_path / ".cursor.json"))
    create_full_chatlog_schema(paths["db_path"])
    return paths["db_path"]


@needs_git
@pytest.mark.asyncio
async def test_ingest_stores_cwd_git_root_branch_and_tree(ingest_env, tmp_path):
    import chatlog_ingest

    repo = _git_repo(tmp_path / "recall-proj", branch="feature/x")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _claude_line("e1", "user", "do the thing", "/not/used") + "\n", encoding="utf-8", newline="\n"
    )
    res = await chatlog_ingest._ingest("claude-code", str(transcript), "", "stop", cwd=str(repo))
    assert res.get("written") == 1, res
    conn = sqlite3.connect(str(ingest_env))
    try:
        (meta,) = conn.execute(
            "SELECT metadata_json FROM memory_items WHERE type='chat_log' AND conversation_id='sess-cap-1'"
        ).fetchone()
    finally:
        conn.close()
    m = json.loads(meta)
    assert m["tree"] == "recall-proj" and m["git_branch"] == "feature/x", m
    assert os.path.normcase(m["git_root"]) == os.path.normcase(os.path.realpath(repo)), m
    assert m["cwd"] == os.path.normpath(str(repo)) and m["host_agent"] == "claude-code", m


@pytest.mark.asyncio
async def test_ingest_cli_hands_cwd_to_the_run(monkeypatch, capsys):
    import chatlog_ingest

    seen = {}

    async def _fake(fmt, path, sid, variant, cwd="", model_id=""):
        seen.update(fmt=fmt, cwd=cwd)
        return {"written": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(chatlog_ingest, "_ingest", _fake)
    monkeypatch.setattr(
        sys, "argv", ["chatlog_ingest", "--format", "codex", "--transcript-path", "x.jsonl", "--cwd", "/w/proj"]
    )
    assert await chatlog_ingest.main() == 0
    assert seen == {"fmt": "codex", "cwd": "/w/proj"}, seen


# ── the hooks pass the envelope's cwd ───────────────────────────────────────


def _load(name):
    spec = importlib.util.spec_from_file_location("_m3_hook_cwd_" + name, os.path.abspath(f"{HOOK_DIR}/{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", HOOKS)
@pytest.mark.parametrize("cwd", ["/w/the-project", ""])
def test_each_hook_passes_cwd_to_ingest_only_when_the_envelope_has_one(name, cwd, monkeypatch, tmp_path):
    mod = _load(name)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "chatlog_ingest.py").write_text("", encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    seen: list[list[str]] = []
    monkeypatch.setattr(mod, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(mod, "find_python", lambda repo: "py")
    monkeypatch.setattr(mod, "run_ingest", lambda py, ingest, args: seen.append(list(args)) or {"rc": 0})
    monkeypatch.setattr(mod, "failure_reason", lambda res, transcript: None)
    monkeypatch.setattr(mod, "note_spill", lambda res, agent: None)
    envelope = {"transcript_path": str(transcript), "session_id": "s1", "hook_event_name": "Stop"}
    if cwd:
        envelope["cwd"] = cwd

    class _Stdin(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(envelope)))
    assert mod.main() == 0
    args = seen[-1]
    if cwd:
        assert args[args.index("--cwd") + 1] == cwd, f"{name}: observed argv {args}"
    else:
        assert "--cwd" not in args, f"{name}: passed --cwd with no cwd in the envelope: {args}"
