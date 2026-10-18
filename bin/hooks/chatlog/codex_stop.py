#!/usr/bin/env python3
"""Codex CLI Stop / SessionEnd hook -> m3-memory chatlog ingest.

Cross-platform (Windows, macOS, Linux). Reads the Codex hook envelope from
stdin, runs chatlog_ingest.py --format codex on the session's rollout file, and
SCREAMS loudly if capture fails, writing a fallback file to ~/.m3/unsaved_chats/
so no session is silently lost.

Stop fires after every turn and re-reads the whole rollout; the ingest cursor
skips turns an earlier fire stored, so a repeat is a success, not a loss.

Envelope (stdin JSON, Codex hooks):
  { "session_id": "...", "turn_id": "...", "transcript_path": "..." | null,
    "cwd": "...", "hook_event_name": "Stop" | "SessionEnd", "reason": "..." }
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

AGENT = "codex"
FORMAT = "codex"


# ── Shared core (parity across all m3 agent hooks) ───────────────────────────
# Byte-identical in every hook (tests/test_chatlog_hooks_success_criterion.py).
# Stdlib only: a hook must run, and scream, even when the m3 package is broken.

# Console-less child on Windows; 0 (the default) where the flag does not exist.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_TAIL = 400  # chars of ingest stderr carried into a failure report


def find_repo_root() -> Path:
    m3_home = os.environ.get("M3_HOME")
    if m3_home:
        return Path(m3_home)
    return Path(__file__).resolve().parents[3]


def usable_interpreter(candidate: Path) -> bool:
    """True only if `candidate` can import the chatlog_ingest dependencies.

    Existence is NOT usability. A bare `python -m venv` (no packages,
    include-system-site-packages=false) satisfies .exists() but dies with
    ModuleNotFoundError the moment chatlog_ingest imports m3_sdk -> httpx.
    That produces no parseable JSON on stdout, so run_ingest() reports the
    generic "m3 ingest failed or unreachable" and every capture is silently
    lost while the hook config still looks correct.

    Regression 2026-08-09..11: a stub `.venv` appeared beside the installed
    payload; find_python() preferred it over the working pipx interpreter and
    killed chatlog capture for ~2 days. The only visible symptom was a stale
    last_write_at. Probe the import instead of trusting the path.
    """
    try:
        return (
            subprocess.run(
                [str(candidate), "-c", "import httpx"],
                capture_output=True,
                timeout=10,
                creationflags=_NO_WINDOW,
            ).returncode
            == 0
        )
    except Exception:
        return False


def find_python(repo: Path) -> str:
    for candidate in [
        repo / ".venv" / "Scripts" / "python.exe",
        repo / ".venv" / "bin" / "python",
    ]:
        if os.path.exists(candidate) and usable_interpreter(candidate):
            return str(candidate)
    # sys.executable is the interpreter already running this hook, which by
    # definition imported everything it needed to get here.
    return sys.executable


def recovery_command(py: str, ingest: Path, transcript: str, session_id: str, variant: str) -> str:
    """The exact command that re-runs this capture: absolute paths, same session."""
    cmd = f'"{py}" "{ingest}" --format {FORMAT} --transcript-path "{transcript}" --variant {variant}'
    if session_id:
        cmd += f" --session-id {session_id}"
    return cmd


def write_fallback(
    transcript: str, session_id: str, agent: str, event: str, reason: str, recover: str, detail: str = ""
) -> Path:
    now = datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    sid = (session_id or "unknown")[:8]
    save_dir = Path.home() / ".m3" / "unsaved_chats"
    save_dir.mkdir(parents=True, exist_ok=True)
    fallback = save_dir / f"m3_unsaved_{agent}_{sid}_{now.strftime('%Y%m%dT%H%M%SZ')}.md"
    content = f"""# M3 CHATLOG NOT SAVED - {stamp}

**Agent:** {agent}
**Event:** {event}
**Session:** {session_id}
**Transcript path:** {transcript}
**Reason:** {reason}

This session's chatlog was NOT captured by m3-memory. The transcript above is
the only copy until the recovery command succeeds.

To recover (re-runs the same capture; already-stored turns are skipped):

    {recover}

To diagnose: m3 chatlog doctor
"""
    if detail:
        content += f"\nIngest stderr (last {_TAIL} chars):\n\n```\n{detail}\n```\n"
    fallback.write_text(content, encoding="utf-8")
    return fallback


def scream(reason: str, fallback: Path, agent: str, event: str) -> None:
    border = "=" * 70
    msg = f"""
{border}
  M3 CHATLOG NOT SAVED - SESSION CONTEXT WILL BE LOST

  Agent  : {agent} ({event})
  Reason : {reason}
  Saved  : {fallback}

  Fix    : run the recovery command in the file above;
           diagnose with `m3 chatlog doctor`.
{border}
"""
    print(msg, file=sys.stderr, flush=True)


def _extract_last_json_object(output: str):
    """Return the last balanced top-level {...} JSON object in `output`, or None.

    ingest interleaves log lines with a final PRETTY-PRINTED JSON object. The old
    parser scanned for a line *starting with* '{' and json.loads'd that single
    line — which is just '{' for pretty JSON, so it always raised and reported
    written=0 (the 1485-bogus-files bug). Scan from the end for the last '}', walk
    backwards tracking brace depth (ignoring braces in strings), json.loads the
    slice. Handles compact single-line and multi-line pretty JSON alike.
    """
    end = output.rfind("}")
    if end == -1:
        return None
    depth = 0
    in_str = False
    for i in range(end, -1, -1):
        ch = output[i]
        if in_str:
            if ch == '"':
                bs = 0
                j = i - 1
                while j >= 0 and output[j] == "\\":
                    bs += 1
                    j -= 1
                if bs % 2 == 0:
                    in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(output[i : end + 1])
                except json.JSONDecodeError:
                    return None
    return None


def run_ingest(py: str, ingest: Path, extra_args: list) -> dict:
    """Run ingest and return its JSON result plus rc, interpreter and stderr tail.

    Keys always present: written, skipped, spilled, pending, failed, error,
    errors, rc, py, stderr_tail. error is None only when ingest reported none.
    """
    out: dict = {
        "written": 0,
        "skipped": 0,
        "spilled": 0,
        "pending": 0,
        "failed": 0,
        "error": None,
        "errors": [],
        "rc": 1,
        "py": py,
        "stderr_tail": "",
    }
    try:
        result = subprocess.run(
            [py, str(ingest), *extra_args], capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW
        )
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        out.update(error="ingest timed out after 60s", stderr_tail=err[-_TAIL:])
        return out
    except Exception as e:
        out.update(error=f"could not start ingest: {type(e).__name__}: {e}")
        return out
    out["rc"] = result.returncode
    out["stderr_tail"] = (getattr(result, "stderr", "") or "")[-_TAIL:]
    # ingest prints PRETTY (multi-line) JSON after its log lines; parse the LAST
    # balanced {...} block (a line-based parse took '{' alone and always failed).
    parsed = _extract_last_json_object((result.stdout or "").strip())
    if not parsed:
        out["error"] = "ingest printed no result JSON (it crashed before reporting)"
        return out
    for key in ("written", "skipped", "spilled", "pending", "failed"):
        try:
            out[key] = int(parsed.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    out["error"] = parsed.get("error") or None
    out["errors"] = list(parsed.get("errors") or [])[:3]
    out["spill_dir"] = parsed.get("spill_dir")
    return out


def failure_reason(res: dict, transcript: str) -> "str | None":
    """None when capture is complete; else one cause, with rc and interpreter.

    Success is DURABILITY, not written>0: skipped counts turns an earlier fire
    for this session already captured (the ingest cursor; Stop fires every turn
    and re-reads the whole transcript), and spilled turns sit in a spill file
    that the cognitive loop drains into the DB.
    """
    if res["error"]:
        cause = res["error"]
    elif res["pending"]:
        cause = f"{res['pending']} turn(s) reached neither the DB nor a spill file"
    elif res["failed"]:
        cause = f"{res['failed']} turn(s) were rejected by validation: {'; '.join(res['errors']) or 'no detail'}"
    elif res["rc"] != 0:
        cause = "ingest exited non-zero without reporting an error"
    elif res["written"] == 0 and res["skipped"] == 0 and res["spilled"] == 0:
        cause = f"ingest found no parseable turns in {transcript} (empty transcript or wrong format)"
    else:
        return None
    return f"{cause} [rc={res['rc']}, interpreter={res['py']}]"


def note_spill(res: dict, agent: str) -> None:
    """A spill is durable, not a loss: say where it is, without screaming."""
    if res["spilled"]:
        print(
            f"{agent} hook: {res['spilled']} turn(s) spilled to {res.get('spill_dir') or 'the chatlog spill dir'} "
            "(DB write failed; the cognitive loop drains it). Check: m3 chatlog status",
            file=sys.stderr,
        )


def report_failure(
    reason: str, res: "dict | None", py: str, ingest: Path, transcript: str, session_id: str, event: str, variant: str
) -> Path:
    """Write the fallback file and scream. Returns the fallback path."""
    recover = recovery_command(py, ingest, transcript, session_id, variant)
    detail = (res or {}).get("stderr_tail", "")
    fallback = write_fallback(transcript, session_id, AGENT, event, reason, recover, detail)
    scream(reason, fallback, AGENT, event)
    return fallback


# ── Agent-specific main ───────────────────────────────────────────────────────


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(f"{AGENT} hook: empty stdin envelope", file=sys.stderr)
        return 1

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"{AGENT} hook: malformed JSON envelope: {e}", file=sys.stderr)
        return 1

    transcript = envelope.get("transcript_path") or ""
    session_id = envelope.get("session_id", "")
    event_name = envelope.get("hook_event_name", "")
    variant = {"Stop": "stop", "SessionEnd": "session_end"}.get(event_name, "codex")

    repo = find_repo_root()
    ingest = repo / "bin" / "chatlog_ingest.py"
    if not transcript:
        # Codex passes null for a session it does not persist; there is nothing
        # on disk to read, and guessing another session's file would be worse.
        reason = "Codex sent no transcript_path (the session is not being saved to disk)"
        report_failure(reason, None, sys.executable, ingest, transcript, session_id, event_name, variant)
        return 1
    if not os.path.exists(ingest):
        reason = f"chatlog_ingest.py not found under {repo} (set M3_HOME to the m3-memory payload root)"
        report_failure(reason, None, sys.executable, ingest, transcript, session_id, event_name, variant)
        return 1

    py = find_python(repo)
    args = ["--format", FORMAT, "--transcript-path", transcript, "--variant", variant]
    if session_id:
        args += ["--session-id", session_id]

    res = run_ingest(py, ingest, args)
    failure = failure_reason(res, transcript)
    if failure:
        report_failure(failure, res, py, ingest, transcript, session_id, event_name, variant)
        # Codex shows a hook's systemMessage in its UI; stderr alone may go unseen.
        print(json.dumps({"systemMessage": f"m3 chatlog NOT saved: {failure}. diagnose: m3 chatlog doctor"}))
        return res["rc"] if res["rc"] != 0 else 1
    note_spill(res, AGENT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
