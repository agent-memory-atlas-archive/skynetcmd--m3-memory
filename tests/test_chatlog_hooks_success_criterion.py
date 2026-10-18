"""Regression tests for the chatlog exit-hook success criterion.

Two real false-positive bugs have hit these hooks, both causing bogus
``~/.m3/unsaved_chats/`` spill files + a "M3 CHATLOG NOT SAVED" scream while
ingest was actually succeeding:

  1. The PRETTY-JSON parser bug (2026-06-04..13, ~1485 bogus files): the parser
     scanned for a line *starting with* "{" and json.loads'd that single line,
     which is just "{" for pretty-printed JSON, so it always reported written=0.
     Fixed in claude_code_precompact.py with _extract_last_json_object(); this
     test pins that the fix is present in ALL THREE hooks.

  2. The written==0-is-failure bug (observed 2026-06-27): the live MCP server
     captures turns itself via chatlog_write, so by the time a PreCompact/Stop/
     SessionEnd hook runs its own ingest, the turns are ALREADY in the DB ->
     ingest legitimately reports written=0, skipped=N (all deduped). That is a
     SUCCESS, not a loss. Treating written==0 as failure screamed every time the
     live capture beat the hook. Success must be REACHABILITY (failed==0, no
     error, something seen), not written>0.

These hooks are three independent hand-maintained copies with a "# Shared core
(parity ...)" comment that promised parity but did not enforce it — which is
exactly why fix #1 reached only the claude hook. The parity guard below makes
divergence a test failure.
"""

from __future__ import annotations

import importlib.util
import os
import sys

HOOK_DIR = os.path.join(os.path.dirname(__file__), "..", "bin", "hooks", "chatlog")
HOOKS = ("claude_code_precompact", "gemini_cli_onexit", "opencode_session_end", "openclaw_session_end", "codex_stop")


def _load(name):
    path = os.path.join(HOOK_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("_m3_hook_" + name, os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Pretty-printed (multi-line) JSON exactly as chatlog_ingest.py emits it, with
# log lines interleaved before it — the shape that broke the old parser.
def _fake_ingest_stdout(written, skipped, failed=0, error=None, with_logs=True):
    import json

    payload = {
        "written": written,
        "skipped": skipped,
        "spilled": 0,
        "failed": failed,
        "errors": [],
        "session_id": "abc123",
    }
    if error is not None:
        payload["error"] = error
    body = json.dumps(payload, indent=2)
    if with_logs:
        return (
            "2026-06-27 22:59:37 - M3_SDK - INFO - SQLite pool ready\n"
            "2026-06-27 22:59:37 - chatlog_ingest - INFO - Ingested foo.jsonl: "
            f"written={written}, skipped={skipped}\n" + body
        )
    return body


class _FakeCompleted:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _patch_subprocess(monkeypatch, mod, stdout, returncode=0, stderr="", calls=None):
    def _run(*a, **k):
        if calls is not None:
            calls.append(a[0])
        return _FakeCompleted(stdout, returncode, stderr)

    monkeypatch.setattr(mod.subprocess, "run", _run)


# ── Parser regression (bug #1): pretty-printed JSON must parse ────────────────


def test_extract_last_json_object_present_in_all_hooks():
    for name in HOOKS:
        mod = _load(name)
        assert hasattr(mod, "_extract_last_json_object"), (
            f"{name} is missing the robust parser — it will hit the pretty-JSON bug that wrote ~1485 bogus spill files"
        )


def test_pretty_json_parses_in_all_hooks():
    pretty = _fake_ingest_stdout(written=9, skipped=1673)
    for name in HOOKS:
        mod = _load(name)
        obj = mod._extract_last_json_object(pretty)
        assert obj is not None, f"{name} failed to parse pretty-printed ingest JSON"
        assert obj["written"] == 9 and obj["skipped"] == 1673


def test_run_ingest_reports_counts_rc_and_interpreter_in_all_hooks(monkeypatch):
    """run_ingest must surface skipped+failed (not just written+error) so the
    caller can tell 'deduped success' from 'real failure', plus what a failure
    report needs: exit code, interpreter, stderr tail."""
    for name in HOOKS:
        mod = _load(name)
        _patch_subprocess(monkeypatch, mod, _fake_ingest_stdout(written=0, skipped=1682), stderr="x" * 900)
        res = mod.run_ingest("py", mod.Path("ingest.py"), [])
        assert (res["written"], res["skipped"], res["failed"], res["error"]) == (0, 1682, 0, None)
        assert res["rc"] == 0 and res["py"] == "py"
        assert len(res["stderr_tail"]) == 400, "stderr tail must be bounded"


# ── Success-criterion regression (bug #2): written==0 + skipped>0 is SUCCESS ──


def test_all_deduped_does_not_scream(monkeypatch, tmp_path, capsys):
    """written=0, skipped=N (live MCP capture beat the hook) -> NO fallback file,
    NO scream, exit 0. This is the 2026-06-27 false-alarm."""
    for name in HOOKS:
        mod = _load(name)
        _patch_subprocess(monkeypatch, mod, _fake_ingest_stdout(written=0, skipped=1682))
        # Redirect the fallback dir into tmp so we can assert nothing is written.
        unsaved = tmp_path / name / ".m3" / "unsaved_chats"
        monkeypatch.setattr(mod.Path, "home", staticmethod(lambda p=tmp_path, n=name: p / n))
        monkeypatch.setattr(sys, "stdin", _StdinStub(name))

        rc = mod.main()
        assert rc == 0, f"{name}: all-deduped run must exit 0, got {rc}"
        assert not unsaved.exists() or not any(unsaved.iterdir()), f"{name}: all-deduped run wrote a bogus spill file"
        err = capsys.readouterr().err
        assert "M3 CHATLOG NOT SAVED" not in err, f"{name}: all-deduped run screamed a false alarm"


def test_genuine_failure_still_screams(monkeypatch, tmp_path, capsys):
    """error set OR failed>0 OR nothing-seen MUST still scream + spill — the
    safety net we must not weaken while fixing the false positive."""
    cases = [
        _fake_ingest_stdout(written=0, skipped=0, error="m3 unreachable"),
        _fake_ingest_stdout(written=0, skipped=0, failed=3),
        _fake_ingest_stdout(written=0, skipped=0),  # nothing seen at all
    ]
    for name in HOOKS:
        for stdout in cases:
            mod = _load(name)
            _patch_subprocess(monkeypatch, mod, stdout, returncode=0)
            unsaved = tmp_path / name / ".m3" / "unsaved_chats"
            monkeypatch.setattr(mod.Path, "home", staticmethod(lambda p=tmp_path, n=name: p / n))
            monkeypatch.setattr(sys, "stdin", _StdinStub(name))

            rc = mod.main()
            assert rc != 0, f"{name}: genuine failure must exit non-zero"
            assert unsaved.exists() and any(unsaved.iterdir()), f"{name}: genuine failure must write a spill file"
            err = capsys.readouterr().err
            assert "M3 CHATLOG NOT SAVED" in err, f"{name}: genuine failure must scream"


def test_normal_write_succeeds_quietly(monkeypatch, tmp_path, capsys):
    """written>0 -> success, no scream, no spill (the happy path)."""
    for name in HOOKS:
        mod = _load(name)
        _patch_subprocess(monkeypatch, mod, _fake_ingest_stdout(written=9, skipped=1673))
        unsaved = tmp_path / name / ".m3" / "unsaved_chats"
        monkeypatch.setattr(mod.Path, "home", staticmethod(lambda p=tmp_path, n=name: p / n))
        monkeypatch.setattr(sys, "stdin", _StdinStub(name))

        rc = mod.main()
        assert rc == 0
        assert not unsaved.exists() or not any(unsaved.iterdir())
        assert "M3 CHATLOG NOT SAVED" not in capsys.readouterr().err


# ── Parity guard: the three hooks must not silently diverge again ─────────────


def test_parser_is_byte_identical_across_hooks():
    import inspect

    srcs = {name: inspect.getsource(_load(name)._extract_last_json_object) for name in HOOKS}
    ref = srcs[HOOKS[0]]
    for name in HOOKS[1:]:
        assert srcs[name] == ref, (
            f"{name}._extract_last_json_object diverged from {HOOKS[0]} — the "
            "'shared core (parity)' promise is broken; re-sync the parser"
        )


class _StdinStub:
    """A minimal stdin whose .read() returns a valid hook envelope for `name`."""

    def __init__(self, name):
        import json

        event = {
            "claude_code_precompact": "PreCompact",
            "gemini_cli_onexit": "SessionEnd",
            "opencode_session_end": "SessionEnd",
            "openclaw_session_end": "command:reset",
            "codex_stop": "Stop",
        }[name]
        self._raw = json.dumps(
            {
                "session_id": "abc123",
                "transcript_path": os.path.abspath(__file__),  # any existing file
                "cwd": os.getcwd(),
                "hook_event_name": event,
            }
        )

    def read(self):
        return self._raw

    def isatty(self):
        return False


# ── The whole shared core, not just the parser, is one text ──────────────────


def _src(name):
    from pathlib import Path

    return Path(HOOK_DIR, name + ".py").read_text(encoding="utf-8")


def _core_text(name):
    src = _src(name)
    start = src.index("# ── Shared core")
    end = src.index("\ndef report_failure(", start)
    end = src.index("\n\n\n", end)
    return src[start:end]


def test_shared_core_is_byte_identical_across_hooks():
    ref = _core_text(HOOKS[0])
    for name in HOOKS[1:]:
        assert _core_text(name) == ref, f"{name}: shared core diverged from {HOOKS[0]}; re-sync it"


def test_hooks_carry_no_os_identity_check():
    """Window suppression is a capability (getattr CREATE_NO_WINDOW), not os.name."""
    for name in HOOKS:
        src = _src(name)
        assert "os.name" not in src and "sys.platform" not in src, name


# ── Durability-aware outcomes ─────────────────────────────────────────────────


def _stdout(**payload):
    import json

    base = {"written": 0, "skipped": 0, "spilled": 0, "pending": 0, "failed": 0, "errors": []}
    base.update(payload)
    return "log line\n" + json.dumps(base, indent=2)


def _run_main(monkeypatch, tmp_path, name, stdout, rc=0, stderr=""):
    mod = _load(name)
    calls: list = []
    _patch_subprocess(monkeypatch, mod, stdout, returncode=rc, stderr=stderr, calls=calls)
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda p=tmp_path, n=name: p / n))
    monkeypatch.setattr(sys, "stdin", _StdinStub(name))
    code = mod.main()
    unsaved = tmp_path / name / ".m3" / "unsaved_chats"
    files = sorted(unsaved.iterdir()) if unsaved.exists() else []
    return code, files, calls


def test_spilled_turns_are_durable_not_a_failure(monkeypatch, tmp_path, capsys):
    for name in HOOKS:
        code, files, _ = _run_main(monkeypatch, tmp_path, name, _stdout(spilled=3, spill_dir="/s/chatlog_spill"))
        err = capsys.readouterr().err
        assert code == 0 and not files, f"{name}: a spill is durable; no fallback, exit 0"
        assert "3 turn(s) spilled to /s/chatlog_spill" in err and "NOT SAVED" not in err


def test_pending_rows_scream(monkeypatch, tmp_path, capsys):
    for name in HOOKS:
        code, files, _ = _run_main(
            monkeypatch, tmp_path, name, _stdout(written=2, pending=1, error="1 row(s) neither reached"), rc=1
        )
        assert code == 1 and files, name
        assert "NOT SAVED" in capsys.readouterr().err


def test_nonzero_exit_without_error_is_a_failure(monkeypatch, tmp_path, capsys):
    for name in HOOKS:
        code, files, _ = _run_main(monkeypatch, tmp_path, name, _stdout(written=4), rc=3)
        assert code == 3 and files, f"{name}: rc=3 must not be reported as success"
        capsys.readouterr()


def test_failure_report_names_rc_interpreter_stderr_and_exact_recovery(monkeypatch, tmp_path, capsys):
    """The fallback carries everything needed to act: cause, rc, interpreter,
    the stderr tail, and a recovery command with absolute interpreter + script
    and the session id. It never says 'restart m3 MCP' (unrelated to capture)."""
    for name in HOOKS:
        code, files, calls = _run_main(
            monkeypatch, tmp_path, name, "Traceback ... boom", rc=2, stderr="E" * 1000 + "LAST-LINE"
        )
        assert code == 2 and len(files) == 1, name
        body = files[0].read_text(encoding="utf-8")
        py = calls[-1][0]
        assert "rc=2" in body and f"interpreter={py}" in body, body
        assert body.rstrip().endswith("```") and "LAST-LINE" in body
        assert f'"{py}"' in body and "chatlog_ingest.py" in body and "--session-id abc123" in body, body
        assert "MCP" not in body
        err = capsys.readouterr().err
        assert "MCP" not in err and "m3 chatlog doctor" in err
        # UTC stamp in the name and the heading (idiom 8)
        assert files[0].name.endswith("Z.md"), files[0].name
        assert "Z\n" in body.splitlines(keepends=True)[0]


def test_opencode_without_transcript_ingests_the_store_via_auto(monkeypatch, tmp_path):
    """OpenCode never sends transcript_path; returning 0 there skipped every capture."""
    mod = _load("opencode_session_end")
    calls: list = []
    _patch_subprocess(monkeypatch, mod, _stdout(written=1), calls=calls)
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))

    class _Empty(_StdinStub):
        def __init__(self):
            self._raw = ""

    monkeypatch.setattr(sys, "stdin", _Empty())
    assert mod.main() == 0
    argv = calls[-1]
    assert argv[argv.index("--transcript-path") + 1] == "auto", argv
