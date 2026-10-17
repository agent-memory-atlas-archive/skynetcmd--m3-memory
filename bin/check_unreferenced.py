#!/usr/bin/env python3
"""Check that every unreferenced symbol is either removed or DECLARED.

vulture (at 100% confidence) lists what nothing in the code reads: parameters,
locals, unreachable branches, always-true/false conditions. That is a fact
about references, not a verdict: an unreferenced parameter can be a host
framework's signature, or a feature's reserved slot. So each finding is either
fixed, or declared below with its reason under one of two kinds:

  INTERFACE  the signature belongs to someone else (a framework, a C API)
  RESERVED   held for a planned feature that is not wired yet (never delete
             one to quiet this tool)

An undeclared finding fails, and a declaration that no longer matches a finding
fails too, so the lists cannot rot.

    python bin/check_unreferenced.py        # needs vulture (pip install vulture)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (path, name) -> whose signature it is
INTERFACE: dict[tuple[str, str], str] = {
    (
        "m3_memory/integrations/langchain/checkpoint.py",
        "new_versions",
    ): "BaseCheckpointSaver.put signature; m3 stores the whole checkpoint, not per-channel blobs",
    (
        "m3_memory/integrations/langchain/checkpoint.py",
        "task_path",
    ): "BaseCheckpointSaver.put_writes signature; writes are keyed by task_id",
    (
        "m3_memory/integrations/langchain/retriever.py",
        "run_manager",
    ): "BaseRetriever._get_relevant_documents signature; m3 emits no callbacks",
    ("bin/m3_core/runtime.py", "interpolate"): "the no-op load_dotenv shim mirrors python-dotenv's signature",
    ("bin/test_fips_integrity.py", "authIn"): "ctypes mock of a wolfCrypt AES-GCM entry point: the C signature",
    ("bin/test_fips_integrity.py", "authInSz"): "ctypes mock of a wolfCrypt AES-GCM entry point: the C signature",
    ("bin/test_fips_integrity.py", "typeH"): "ctypes mock of a wolfCrypt HMAC entry point: the C signature",
    ("bin/test_fips_integrity.py", "nonce_sz"): "ctypes mock of wolfCrypt wc_rng_new: the C signature",
    ("bin/test_fips_integrity.py", "heap"): "ctypes mock of wolfCrypt wc_rng_new: the C signature",
    (
        "bin/test_fips_integrity.py",
        "rng",
    ): "ctypes mock of wolfCrypt wc_rng_free / wc_RNG_GenerateBlock: the C signature",
}

# (path, name) -> the planned feature it is held for
RESERVED: dict[tuple[str, str], str] = {
    (
        "bin/files_memory/extract.py",
        "leaf_division_type",
    ): "a prompt hint for `window` leaves, adopted only once measured",
    ("bin/memory_maintenance.py", "output_format"): "export formats beyond JSON; JSON is the only one today",
}

DECLARED = {**INTERFACE, **RESERVED}

_FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<msg>.*?'(?P<name>[^']+)'.*?)$")


def main() -> int:
    if subprocess.run([sys.executable, "-m", "vulture", "--version"], capture_output=True).returncode != 0:
        print("check_unreferenced: observed: vulture is not installed. inspect: pip install vulture", file=sys.stderr)
        return 2
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vulture",
            "bin",
            "m3_memory",
            "--min-confidence",
            "100",
            "--exclude",
            "*/bench_*,*/benchmark_*,*/__pycache__/*",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode not in (0, 3):  # 3 = findings; anything else is vulture failing
        print(
            f"check_unreferenced: observed: vulture exited {proc.returncode}: "
            f"{proc.stderr.strip()[:500]} inspect: run by hand: {' '.join(proc.args)}",
            file=sys.stderr,
        )
        return proc.returncode or 1
    seen: set[tuple[str, str]] = set()
    undeclared: list[str] = []
    for line in proc.stdout.splitlines():
        m = _FINDING.match(line.strip())
        if not m:
            undeclared.append(line)
            continue
        key = (m["path"].replace("\\", "/"), m["name"])
        if key in DECLARED:
            seen.add(key)
        else:
            undeclared.append(f"{m['path']}:{m['line']}: {m['name']!r} is referenced by nothing")
    stale = sorted(set(DECLARED) - seen)
    for line in undeclared:
        print(
            f"check_unreferenced: observed: {line}. inspect: remove it if nothing intends it; "
            "if it is a host's signature declare it INTERFACE, if it is held for a planned "
            "feature declare it RESERVED (bin/check_unreferenced.py)",
            file=sys.stderr,
        )
    for path, name in stale:
        print(
            f"check_unreferenced: observed: ({path}, {name}) is declared but now referenced or "
            "gone. inspect: remove the declaration",
            file=sys.stderr,
        )
    if undeclared or stale:
        return 1
    print(f"check_unreferenced: clean ({len(INTERFACE)} interface, {len(RESERVED)} reserved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
