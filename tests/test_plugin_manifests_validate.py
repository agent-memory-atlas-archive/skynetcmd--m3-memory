"""CI guard: m3's published plugins must pass their HOST's OWN validator.

Rather than checking plugin manifests against a hand-maintained schema (or against
how some peer plugin happens to be structured), run each host's authoritative
validator against m3's plugin dir:

  * Claude Code:  `claude plugin validate .`      (validates .claude-plugin/)
  * Antigravity:  `agy plugin validate plugins/antigravity`
  * Gemini CLI:   `gemini extensions validate .`  (gemini-extension.json)

Both are skipped when the binary isn't on PATH (CI images may lack them), so this
is a guard that fires wherever the tooling exists, not a hard dependency.

IMPORTANT — exit codes are UNRELIABLE: both `claude plugin validate` and
`agy plugin validate` print "Validation failed" / "Error:" but still exit 0
(verified 2026-07-21). So we assert on OUTPUT MARKERS, never the return code —
a naive `returncode == 0` check would pass a broken manifest.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import skip_reason

_ROOT = Path(__file__).resolve().parents[1]


_TIMEOUT_S = 60


def _run(cmd: list[str], cwd: Path) -> str:
    """Run a validator and return combined stdout+stderr (exit code ignored).

    A validator that never answers is an environment condition, not a manifest
    verdict: `claude plugin validate` hangs past 90 s in non-interactive shells
    (nohup, ssh). That skips with its reason; a real validation failure still
    fails, because that is judged on the output below."""
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        pytest.skip(
            skip_reason(
                "env",
                f"{cmd[0]} plugin validate gave no answer; observed: no exit within {_TIMEOUT_S} s. "
                "inspect: run it in an interactive shell (it hangs under nohup/ssh)",
            )
        )
    return (p.stdout or "") + (p.stderr or "")


def test_a_hung_validator_skips_rather_than_fails(monkeypatch):
    """The timeout path is an env skip, not a failure (no validator binary needed)."""

    def _hang(*_a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude plugin validate .", timeout=kw.get("timeout"))

    monkeypatch.setattr(subprocess, "run", _hang)
    with pytest.raises(pytest.skip.Exception) as exc:
        _run(["claude", "plugin", "validate", "."], _ROOT)
    assert str(exc.value.msg).startswith("[env]"), "the skip must name its dimension"
    assert "observed:" in str(exc.value.msg)


@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason=skip_reason("env", "claude binary not on PATH — plugin validator unavailable"),
)
def test_claude_plugin_validates():
    """`claude plugin validate .` must report success for m3's .claude-plugin.

    Fails on the FAILURE marker, not the (unreliable) exit code."""
    out = _run(["claude", "plugin", "validate", "."], _ROOT)
    assert "Validation failed" not in out, f"claude plugin validate failed:\n{out}"
    assert "Validation passed" in out, f"claude plugin validate did not confirm success:\n{out}"


@pytest.mark.skipif(
    shutil.which("agy") is None, reason=skip_reason("env", "agy binary not on PATH — Antigravity validator unavailable")
)
def test_antigravity_plugin_validates():
    """The Antigravity plugin (plugins/antigravity, installed with
    `agy plugin install <that folder>`) must validate AND load m3's parts. agy
    reads them from the plugin root only and reports others "never loaded", so a
    passing verdict alone proves nothing: the component counts are the check."""
    import json

    out = _run(["agy", "plugin", "validate", str(_ROOT / "plugins" / "antigravity"), "--json"], _ROOT)
    report = json.loads(out[out.index("{") : out.rindex("}") + 1])
    errors = [f for f in report["findings"] if f["severity"] == "error"]
    unloaded = [f["file"] for f in report["findings"] if "never loaded" in f["message"]]
    assert not errors, f"agy plugin validate reported errors: {errors}"
    c = report["components"]
    assert c["hooks"] >= 1 and c["mcpServers"] >= 1 and c["agents"] >= 2 and c["skills"] >= 16, (
        f"the installed plugin would load only {c}; parts agy does not read: {unloaded}"
    )


@pytest.mark.skipif(
    shutil.which("gemini") is None,
    reason=skip_reason("env", "gemini binary not on PATH — extension validator unavailable"),
)
def test_gemini_extension_validates():
    """`gemini extensions validate .` must confirm the root gemini-extension.json.
    Its warning that m3's YYYY.M.D.N version is not semver is expected. The
    resolved path is run: npm installs `gemini.cmd` on Windows, which
    CreateProcess does not find by its bare name."""
    gemini = shutil.which("gemini")
    assert gemini
    out = _run([gemini, "extensions", "validate", str(_ROOT)], _ROOT)
    assert "has been successfully validated" in out, f"gemini extensions validate did not confirm success:\n{out}"
    assert "Error" not in out, f"gemini extensions validate reported an error:\n{out}"


@pytest.mark.skipif(shutil.which("claude") is None, reason=skip_reason("env", "claude binary not on PATH"))
def test_claude_marketplace_validates():
    """The marketplace manifest itself must validate (users add it via
    `/plugin marketplace add skynetcmd/m3-memory`)."""
    out = _run(["claude", "plugin", "validate", str(_ROOT / ".claude-plugin" / "marketplace.json")], _ROOT)
    assert "Validation failed" not in out, f"marketplace validate failed:\n{out}"
    assert "Validation passed" in out, f"marketplace did not validate:\n{out}"
