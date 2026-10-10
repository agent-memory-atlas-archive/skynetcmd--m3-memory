#!/usr/bin/env python3
"""Check for dead code with vulture: the one owner of the invocation.

Runs vulture at 100% confidence (unused variables and parameters, unreachable
code, always-true/false conditions), which is precise enough to gate on. Every
exception is keyed by (file, name) WITH its reason; an unlisted finding fails,
and an exception that no longer fires fails too, so the list cannot rot.

    python bin/check_dead_code.py        # needs vulture (pip install vulture)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (path, name) -> why it is not a finding
EXCEPTIONS: dict[tuple[str, str], str] = {
    # Interface parameters: the signature belongs to a host framework.
    ("m3_memory/integrations/langchain/checkpoint.py", "new_versions"):
        "BaseCheckpointSaver.put signature; m3 stores the whole checkpoint, not per-channel blobs",
    ("m3_memory/integrations/langchain/checkpoint.py", "task_path"):
        "BaseCheckpointSaver.put_writes signature; writes are keyed by task_id",
    ("m3_memory/integrations/langchain/retriever.py", "run_manager"):
        "BaseRetriever._get_relevant_documents signature; m3 emits no callbacks",
    ("bin/m3_core/runtime.py", "interpolate"):
        "the no-op load_dotenv shim mirrors python-dotenv's signature",
    ("bin/test_fips_integrity.py", "authIn"):
        "ctypes mock of a wolfCrypt AES-GCM entry point: the C signature",
    ("bin/test_fips_integrity.py", "authInSz"):
        "ctypes mock of a wolfCrypt AES-GCM entry point: the C signature",
    ("bin/test_fips_integrity.py", "typeH"):
        "ctypes mock of a wolfCrypt HMAC entry point: the C signature",
    # RESERVED PARAMETERS, not dead code: part of the interface, held for a planned
    # feature that is not wired yet. vulture cannot tell the difference, so they are
    # listed here; never delete one to quiet the tool (user rule, 2026-10-10).
    ("bin/files_memory/extract.py", "leaf_division_type"):
        "planned: hint the extraction prompt for `window` leaves (refactor to-do, m3)",
    ("bin/memory_maintenance.py", "output_format"):
        "planned: export formats beyond JSON (refactor to-do, m3); JSON is the only one today",
}

_FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<msg>.*?'(?P<name>[^']+)'.*?)$")


def main() -> int:
    if subprocess.run([sys.executable, "-m", "vulture", "--version"],
                      capture_output=True).returncode != 0:
        print("check_dead_code: observed: vulture is not installed. inspect: pip install vulture",
              file=sys.stderr)
        return 2
    proc = subprocess.run(
        [sys.executable, "-m", "vulture", "bin", "m3_memory", "--min-confidence", "100",
         "--exclude", "*/bench_*,*/benchmark_*,*/__pycache__/*"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode not in (0, 3):  # 3 = findings; anything else is vulture failing
        print(f"check_dead_code: observed: vulture exited {proc.returncode}: "
              f"{proc.stderr.strip()[:500]} inspect: run the command above by hand", file=sys.stderr)
        return proc.returncode or 1
    seen: set[tuple[str, str]] = set()
    unlisted: list[str] = []
    for line in proc.stdout.splitlines():
        m = _FINDING.match(line.strip())
        if not m:
            unlisted.append(line)
            continue
        key = (m["path"].replace("\\", "/"), m["name"])
        if key in EXCEPTIONS:
            seen.add(key)
        else:
            unlisted.append(line)
    stale = sorted(set(EXCEPTIONS) - seen)
    for line in unlisted:
        print(f"check_dead_code: observed: {line} inspect: remove it, or add an "
              f"EXCEPTIONS entry with its reason", file=sys.stderr)
    for path, name in stale:
        print(f"check_dead_code: observed: exception ({path}, {name}) no longer fires. "
              f"inspect: delete it from EXCEPTIONS", file=sys.stderr)
    if unlisted or stale:
        return 1
    print(f"check_dead_code: clean ({len(seen)} listed exceptions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
