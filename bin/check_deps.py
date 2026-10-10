#!/usr/bin/env python3
"""Check declared vs imported dependencies with deptry: the one owner of the invocation.

bin/ is a flat directory of first-party scripts and packages imported by bare
name, which deptry cannot infer, so the first-party list is computed here from
the tree (a hand-kept list would fail on every new script). Every exception is
listed below WITH its reason; an unlisted finding fails.

    python bin/check_deps.py        # needs deptry (pip install deptry)

CI runs it after installing requirements.txt, so deptry can map installed
distributions to import names (yaml -> PyYAML, PIL -> pillow, ...).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

# rule -> {module: why it is not a finding}
EXCEPTIONS: dict[str, dict[str, str]] = {
    # DEP001: imported but not declared
    "DEP001": {
        "m3_core_rs": "the optional native wheel, installed by `m3 setup` from GitHub releases",
        "wolfssl": "optional FIPS backend, built by install_wolfssl.py",
        "fitz": "optional PyMuPDF for the PDF chunker; guarded import",
        "tiktoken": "optional, measure_tool_tokens only; guarded import",
        "polars": "optional fast path in memory.history; guarded import",
        "wmi": "optional Windows thermal probe; guarded import",
        "prompt_toolkit": "aider_patch runs inside aider, which provides it",
        "aider": "aider_patch runs inside aider",
        "agent": "Hermes host module: the plugin runs inside Hermes",
        "tools": "Hermes host module: the plugin runs inside Hermes",
        "utils": "Hermes host module: the plugin runs inside Hermes",
        "orchestrator": "Hermes host module: the plugin runs inside Hermes",
        "hermes_constants": "Hermes host module: the plugin runs inside Hermes",
    },
    # DEP002: declared but not imported by the scanned code
    "DEP002": {
        "fastmcp": "kept on purpose to adopt FastMCP features (m3 to-do 8b40e9e7)",
        "pillow": "floor for fpdf2's image path (docs/DEPENDENCY_NOTES.md)",
        "urllib3": "security floor for requests (docs/DEPENDENCY_NOTES.md)",
        "pyobjc-framework-CoreWLAN": "macOS only; imported as CoreWLAN where installed",
        "transformers": "test group / embed-server: used by tests, which are not scanned",
        "scipy": "test group / embed-server: used by tests, which are not scanned",
        "pydantic-ai-slim": "pydantic-ai extra, imported as pydantic_ai where installed",
        "psycopg": "test group: the PostgreSQL lanes, driven from tests",
        "setuptools": "test group: the CVE floor (docs/DEPENDENCY_NOTES.md)",
    },
}


def first_party() -> list[str]:
    names = {p.stem for p in BIN.glob("*.py")} | {p.name for p in BIN.iterdir() if os.path.isdir(p)}
    return [*sorted(n for n in names if n.isidentifier()), "m3_memory"]


def main() -> int:
    if shutil.which("deptry") is None and subprocess.run(
            [sys.executable, "-m", "deptry", "--version"], capture_output=True).returncode != 0:
        print("check_deps: observed: deptry is not installed. inspect: pip install deptry",
              file=sys.stderr)
        return 2
    ignores = ",".join(f"{rule}={'|'.join(mods)}" for rule, mods in EXCEPTIONS.items())
    cmd = [sys.executable, "-m", "deptry", "bin", "m3_memory"]
    for name in first_party():  # one flag per name: the option takes a single value
        cmd += ["--known-first-party", name]
    cmd += ["--optional-dependencies-dev-groups", "dev",
           "--per-rule-ignores", ignores,
           # deptry matches from the start of the relative path, hence the leading .*
           "--extend-exclude", r"tests|.*test_[^/\\]*\.py|.*bench_.*|.*benchmark_.*"]
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
