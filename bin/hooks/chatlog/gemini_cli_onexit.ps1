# Gemini CLI SessionEnd hook -> m3-memory chat log ingest (Windows).
#
# Envelope on stdin (per gemini-cli docs/hooks/reference.md, Base input schema):
#   { "session_id": "...", "transcript_path": "...",
#     "cwd": "...", "hook_event_name": "SessionEnd", "timestamp": "...",
#     "reason": "exit" | "clear" | "logout" | "prompt_input_exit" | "other" }
#
# Gemini's SessionEnd is fire-and-forget (CLI does not wait), and historically
# fired twice on exit (gemini-cli#18019, since fixed). Idempotency is handled
# downstream by the per-session UUID cursor in chatlog_ingest.py.

$ErrorActionPreference = "Stop"

# Resolve repo root: $M3_HOME wins, else script-relative (..\..\..).
$base = if ($env:M3_HOME) { $env:M3_HOME } else { Resolve-Path (Join-Path $PSScriptRoot "..\..\..") }

$hook = Join-Path $base "bin\hooks\chatlog\gemini_cli_onexit.py"
if (-not (Test-Path $hook)) {
    Write-Error "gemini_cli_onexit: hook script missing at '$hook'. Set M3_HOME to the m3-memory payload root, or reinstall."
    exit 1
}

# See claude_code_precompact.ps1 for why each candidate is PROBED rather than
# merely Test-Path'd: a dependency-less venv passes Test-Path and then dies at
# `import httpx`, silently killing capture.
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

if (-not $py) {
    $py = "python"
    if (-not (Test-M3Python $py)) {
        Write-Warning "gemini_cli_onexit: no python with httpx found; trying '$py' anyway"
    }
}

# Read stdin: prefer the automatic $input enumerator (works for PowerShell
# pipelines), fall back to [Console]::In for a non-PowerShell parent.
$raw = ($input | Out-String)
if ([string]::IsNullOrWhiteSpace($raw)) {
    $raw = [Console]::In.ReadToEnd()
}

# Hand off to the .py hook: it owns the success criterion and, on a failure,
# writes ~/.m3/unsaved_chats/<file> with the recovery command, screams and
# exits non-zero. Calling chatlog_ingest.py directly reported nothing.
# UTF-8 both ways: Windows PowerShell pipes to native programs in
# $OutputEncoding (ASCII by default) and Python reads a pipe in the ANSI code
# page; either would mangle a non-ASCII transcript path.
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$raw | & $py $hook
exit $LASTEXITCODE
