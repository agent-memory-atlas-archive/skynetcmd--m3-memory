#!/usr/bin/env sh
# Claude Code PreCompact / Stop hook → m3-memory chat log ingest.
#
# Envelope on stdin (per https://code.claude.com/docs/en/hooks.md):
#   { "session_id": "...", "transcript_path": "...",
#     "cwd": "...", "hook_event_name": "PreCompact" | "Stop", ... }

# Resolve repo root: $M3_HOME wins, else script-relative (../../..).
if [ -n "$M3_HOME" ]; then
    BASE="$M3_HOME"
else
    BASE="$(cd "$(dirname "$0")/../../.." && pwd)"
fi

if [ ! -f "$BASE/bin/chatlog_ingest.py" ]; then
    echo "claude_code_precompact: could not find bin/chatlog_ingest.py under '$BASE'. Set M3_HOME to the m3-memory repo root." >&2
    exit 1
fi

# Pick a python that has the chatlog_ingest deps (httpx, etc).
#
# EXECUTABLE IS NOT USABLE. A bare `python -m venv` (no packages) passes -x but
# dies at `import httpx` before chatlog_ingest prints any JSON, so the caller
# sees a generic "ingest failed or unreachable" and capture is silently lost.
# That is the 2026-08-09..11 regression: a stub .venv at candidate 1 shadowed
# all eight working fallbacks below it for ~2 days. Probe each candidate and
# take the first that actually imports the dependency.
#
# Priority:
#   1. Repo-local venv at $BASE/.venv (dev checkouts)
#   2. pipx venv for m3-memory:
#      -. Windows (Git Bash): ~/pipx/venvs/m3-memory, %LOCALAPPDATA%/pipx/pipx/venvs/m3-memory
#      a. ~/.local/share/pipx/venvs/m3-memory  (pipx >= 1.4, XDG-compliant — Debian 13, Ubuntu 24.04+)
#      b. ~/.local/pipx/venvs/m3-memory        (pipx < 1.4 — Debian 12, older distros)
#   3. $PIPX_HOME/venvs/m3-memory if user set a custom pipx home
#   4. Env override $M3_PYTHON (last resort for odd layouts)
#   5. system python3 (works only if the host happens to have httpx installed)
m3_usable() {
    [ -n "$1" ] && [ -x "$1" ] && "$1" -c "import httpx" >/dev/null 2>&1
}

# WINDOWS: pythonw.exe BEFORE python.exe at every location.
# Both are the same interpreter; python.exe is the CONSOLE build, so each
# invocation from a hook allocates a conhost and FLASHES A WINDOW that steals
# focus. This wrapper runs up to two interpreters per hook fire (the
# usable-probe and the hook exec), and the Stop hook fires on every turn - so
# the console build produced a visible flash every few seconds (observed
# 2026-09-13).
# The Python hooks already pass CREATE_NO_WINDOW for exactly this reason; a
# shell script has no such flag, so the fix is choosing the windowless binary.
# Verified 2026-09-13 that pythonw.exe is correct for all three call shapes
# here: exit-code probes, stdout through a pipe, and stderr through a pipe all
# behave identically (the "pythonw has no stdout" caveat applies to an
# inherited console, not to redirected handles). python.exe is KEPT as the
# next candidate at each location so a layout without pythonw still resolves.
PY=""
for _cand in \
    "$BASE/.venv/bin/python" \
    "$BASE/.venv/Scripts/pythonw.exe" \
    "$BASE/.venv/Scripts/python.exe" \
    "$HOME/.local/share/pipx/venvs/m3-memory/bin/python" \
    "$HOME/pipx/venvs/m3-memory/Scripts/pythonw.exe" \
    "$HOME/pipx/venvs/m3-memory/Scripts/python.exe" \
    "${LOCALAPPDATA:+$LOCALAPPDATA/pipx/pipx/venvs/m3-memory/Scripts/pythonw.exe}" \
    "${LOCALAPPDATA:+$LOCALAPPDATA/pipx/pipx/venvs/m3-memory/Scripts/python.exe}" \
    "$HOME/.local/share/pipx/venvs/m3-memory/Scripts/pythonw.exe" \
    "$HOME/.local/share/pipx/venvs/m3-memory/Scripts/python.exe" \
    "$HOME/.local/pipx/venvs/m3-memory/bin/python" \
    "$HOME/.local/pipx/venvs/m3-memory/Scripts/pythonw.exe" \
    "$HOME/.local/pipx/venvs/m3-memory/Scripts/python.exe" \
    "${PIPX_HOME:+$PIPX_HOME/venvs/m3-memory/bin/python}" \
    "${PIPX_HOME:+$PIPX_HOME/venvs/m3-memory/Scripts/pythonw.exe}" \
    "${PIPX_HOME:+$PIPX_HOME/venvs/m3-memory/Scripts/python.exe}" \
    "$M3_PYTHON"
do
    if m3_usable "$_cand"; then PY="$_cand"; break; fi
done

# Last resort: system python3. Probe it too, but fall back to it regardless —
# a broken python3 yields a loud ingest error, which beats exiting silently.
if [ -z "$PY" ]; then
    PY="python3"
    m3_usable "$PY" || echo "claude_code_precompact: no python with httpx found; trying '$PY' anyway" >&2
fi

# Hand off to the .py hook (stdin, the host's JSON envelope, is inherited).
# It owns the success criterion: it parses ingest's result, and on a failure
# writes ~/.m3/unsaved_chats/<file> with the recovery command, screams on
# stderr and exits non-zero. Exec'ing chatlog_ingest.py directly left a
# failed capture with nothing but an exit code nobody read.
HOOK="$BASE/bin/hooks/chatlog/claude_code_precompact.py"
if [ ! -f "$HOOK" ]; then
    echo "claude_code_precompact: hook script missing at $HOOK (reinstall m3-memory)" >&2
    exit 1
fi
exec "$PY" "$HOOK"
