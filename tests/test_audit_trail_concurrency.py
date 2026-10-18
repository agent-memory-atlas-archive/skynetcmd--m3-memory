"""The audit trail is one hash chain across processes: concurrent writers must
neither lose entries nor fork the chain (measured before the lock: 6 processes,
155 of 600 entries lost, 276 forks), and a corrupt last line must raise rather
than restart the chain from genesis."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

BIN = os.path.join(os.path.dirname(__file__), "..", "bin")

_CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
import audit_trail
audit_trail.get_audit_trail_path = lambda: sys.argv[2]
for i in range(int(sys.argv[3])):
    audit_trail.write_audit_entry("probe", f"{sys.argv[4]}-{i}", {})
"""


def test_concurrent_processes_keep_one_unbroken_chain(tmp_path, monkeypatch):
    import audit_trail

    log = str(tmp_path / "audit.jsonl")
    n_procs, per = 4, 40
    env = dict(os.environ)
    # Each child's stderr goes to a file (a pipe left undrained can block a child),
    # so a child that dies says why: under pytest's capture it was otherwise lost.
    errs = [tmp_path / f"p{k}.err" for k in range(n_procs)]
    procs = []
    for k, err in enumerate(errs):
        with err.open("w", encoding="utf-8") as fh:
            procs.append(
                subprocess.Popen([sys.executable, "-c", _CHILD, BIN, log, str(per), f"p{k}"], env=env, stderr=fh)
            )
    codes = [p.wait(timeout=180) for p in procs]
    failed = {
        f"p{k}": (rc, errs[k].read_text(encoding="utf-8", errors="replace")[-2000:])
        for k, rc in enumerate(codes)
        if rc != 0
    }
    assert not failed, f"writer processes failed (exit code, stderr tail): {failed}"
    with open(log, encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    assert len(entries) == n_procs * per, "entries were lost"
    monkeypatch.setattr(audit_trail, "get_audit_trail_path", lambda: log)
    assert audit_trail.verify_audit_trail(), "the chain forked or broke"


def test_a_corrupt_last_line_raises_instead_of_forking(tmp_path, monkeypatch):
    import audit_trail

    log = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit_trail, "get_audit_trail_path", lambda: str(log))
    audit_trail.write_audit_entry("a", "x", {})
    with open(log, "a", encoding="utf-8") as f:
        f.write('{"truncated": \n')
    before = log.read_bytes()
    with pytest.raises(RuntimeError, match="not a valid audit entry"):
        audit_trail.write_audit_entry("b", "y", {})
    assert log.read_bytes() == before, "nothing may be appended after a corrupt tail"


def test_the_tail_read_finds_the_last_hash_in_a_long_line_file(tmp_path, monkeypatch):
    import audit_trail

    log = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit_trail, "get_audit_trail_path", lambda: str(log))
    big = {"blob": "z" * 20000}  # one entry longer than the first tail chunk
    audit_trail.write_audit_entry("big", "1", big)
    audit_trail.write_audit_entry("big", "2", big)
    assert audit_trail.verify_audit_trail()
