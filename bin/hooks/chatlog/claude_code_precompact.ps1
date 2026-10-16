# Claude Code PreCompact / Stop hook -> m3-memory chat log ingest (Windows).
#
# Envelope on stdin (per https://code.claude.com/docs/en/hooks.md):
#   { "session_id": "...", "transcript_path": "...",
#     "cwd": "...", "hook_event_name": "PreCompact" | "Stop", ... }
#
# Thin launcher: picks a usable interpreter and hands the envelope to
# claude_code_precompact.py, which runs ingest, parses its result and, on a
# failure, writes ~/.m3/unsaved_chats/<file> with the recovery command, screams
# and exits non-zero. A second copy of that logic here drifted (it parsed the
# first '{' line of pretty JSON, treated written==0 as failure and wrote its
# fallback under ~/.claude while exiting 0).

$ErrorActionPreference = "Stop"

# Resolve repo root: $M3_HOME wins, else script-relative (..\..\..).
$base = if ($env:M3_HOME) { $env:M3_HOME } else { Resolve-Path (Join-Path $PSScriptRoot "..\..\..") }
$hook = Join-Path $base "bin\hooks\chatlog\claude_code_precompact.py"

if (-not (Test-Path $hook)) {
    Write-Error "claude_code_precompact: hook script missing at '$hook'. Set M3_HOME to the m3-memory payload root, or reinstall."
    exit 1
}

# Pick a python that has the chatlog_ingest deps (httpx, etc).
#
# Test-Path IS NOT ENOUGH. A bare `python -m venv` (no packages) passes it and
# then dies at `import httpx` before chatlog_ingest prints any JSON, so the
# caller sees a generic "ingest failed or unreachable" and capture is silently
# lost. That is the 2026-08-09..11 regression: a stub .venv beside the payload
# shadowed the working pipx interpreter for ~2 days. Probe, don't assume.
function Test-M3Python($candidate) {
    if (-not $candidate) { return $false }
    if ($candidate -ne "python" -and -not (Test-Path $candidate)) { return $false }
    try {
        & $candidate -c "import httpx" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$py = $null
foreach ($cand in @(
    (Join-Path $base ".venv\Scripts\python.exe"),
    "$env:USERPROFILE\pipx\venvs\m3-memory\Scripts\python.exe",
    "$env:LOCALAPPDATA\pipx\pipx\venvs\m3-memory\Scripts\python.exe",
    $(if ($env:PIPX_HOME) { Join-Path $env:PIPX_HOME "venvs\m3-memory\Scripts\python.exe" }),
    $env:M3_PYTHON
)) {
    if (Test-M3Python $cand) { $py = $cand; break }
}

# Last resort: whatever `python` is on PATH. Probe it too, but use it either
# way: the .py hook then fails loudly, which beats exiting silently.
if (-not $py) {
    $py = "python"
    if (-not (Test-M3Python $py)) {
        Write-Warning "claude_code_precompact: no python with httpx found; trying '$py' anyway"
    }
}

# Read stdin: prefer the automatic $input enumerator (works for PowerShell
# pipelines), fall back to [Console]::In for a non-PowerShell parent (real
# stdin from the host CLI process).
$raw = ($input | Out-String)
if ([string]::IsNullOrWhiteSpace($raw)) {
    $raw = [Console]::In.ReadToEnd()
}

# UTF-8 both ways: Windows PowerShell pipes to native programs in
# $OutputEncoding (ASCII by default), and Python reads a pipe in the ANSI code
# page; either would mangle a non-ASCII transcript path.
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

$raw | & $py $hook
exit $LASTEXITCODE
