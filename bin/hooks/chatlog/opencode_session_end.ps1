# OpenCode session-end hook → m3-memory chat log ingest (Windows).
$here = $PSScriptRoot
$base = Resolve-Path (Join-Path $here "..\..\..")
# See claude_code_precompact.ps1: Test-Path alone accepts a dependency-less
# venv that then dies at `import httpx`, silently killing capture.
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
        Write-Warning "opencode_session_end: no python with httpx found; trying '$py' anyway"
    }
}

# Hand off to the .py hook: it passes --transcript-path auto (OpenCode writes
# no transcript file; chatlog_ingest owns the per-OS store lookup) and owns the
# failure handling: fallback file with the recovery command, scream, non-zero
# exit. Calling chatlog_ingest.py directly reported nothing on a failure.
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"
$input | & $py (Join-Path $base "bin\hooks\chatlog\opencode_session_end.py")
exit $LASTEXITCODE
