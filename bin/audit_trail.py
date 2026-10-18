#!/usr/bin/env python3
"""
Cryptographically signed, tamper-evident audit trail for m3-memory.
Logs all destructive and mutating operations in a SHA-256 chain-of-trust log.
"""

import json
import os
from datetime import UTC, datetime
from typing import Any

from crypto_provider import get_sha256
from m3_core.runtime import utc_iso
from m3_sdk import get_m3_root


def get_audit_trail_path() -> str:
    """Returns the path to the secure audit trail log file."""
    return os.path.join(get_m3_root(), "audit_trail.log.jsonl")


def write_audit_entry(action: str, target_id: str, metadata: dict[str, Any]) -> str:
    """Appends a new cryptographically signed entry to the audit trail log.

    Each entry carries the SHA-256 hash of the previous entry, forming a
    tamper-evident hash chain.

    Returns the SHA-256 hash of the newly written entry.
    """
    audit_file = get_audit_trail_path()

    # 1. Ensure directory exists
    os.makedirs(os.path.dirname(audit_file), exist_ok=True)

    # Read-last-hash and append are one critical section across processes: without
    # the lock, concurrent writers read the same tail and fork the chain, and
    # interleaved appends lost entries (6 processes: 155 of 600 lost, 276 forks).
    from m3_core.locking import file_lock

    with file_lock(audit_file + ".lock", purpose="audit trail"):
        return _append_entry(audit_file, action, target_id, metadata)


def _last_hash(audit_file: str) -> str:
    """The last entry's hash, read from the file's tail; genesis (64 zeros) for an
    empty or absent file. A last line that does not parse raises: chaining from
    genesis instead would fork the chain silently and hide the damage (§3)."""
    try:
        size = os.path.getsize(audit_file)
    except FileNotFoundError:
        return "0" * 64
    if size == 0:
        return "0" * 64
    with open(audit_file, "rb") as f:
        chunk = 4096
        tail = b""
        pos = size
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            tail = f.read(step) + tail
            if tail.rstrip(b"\r\n").count(b"\n") >= 1:
                break
            chunk *= 2
    last_line = tail.rstrip(b"\r\n").rsplit(b"\n", 1)[-1].strip()
    if not last_line:
        return "0" * 64
    try:
        last_entry = json.loads(last_line)
        return str(last_entry["hash"])
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(
            f"observed: the last line of {audit_file} is not a valid audit entry ({type(e).__name__}); "
            "the chain cannot continue from it. inspect: m3 doctor / verify_audit_trail; "
            "fix: move the file aside to start a new chain, keeping the old one as evidence"
        ) from e


def _append_entry(audit_file: str, action: str, target_id: str, metadata: dict[str, Any]) -> str:
    # 2. Find previous entry's hash (Genesis is 64 zeros)
    prev_hash = _last_hash(audit_file)

    # 3. Create current entry
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "action": action,
        "target_id": target_id,
        "metadata": metadata,
        "prev_hash": prev_hash,
    }

    # 4. Generate canonical string representation (sorted keys, no spaces) for hashing.
    # Route through crypto_provider so the audit chain honors the FIPS boundary
    # (M3_FIPS_MODE -> wolfCrypt). SHA-256 hex is byte-identical to the prior
    # hashlib path on the DEFAULT backend, so existing log entries stay verifiable.
    canonical_str = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    current_hash = get_sha256(canonical_str.encode("utf-8"))

    # 5. Append signed entry to log
    entry["hash"] = current_hash
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    return current_hash


def verify_audit_trail() -> bool:
    """Verifies the integrity of the audit trail log.

    Checks that every entry's hash matches its contents, and that the prev_hash
    of each entry correctly links to the hash of the preceding entry.

    Returns True if the log is consistent, False otherwise.
    """
    audit_file = get_audit_trail_path()
    if not os.path.exists(audit_file):
        return True  # Empty log is technically consistent

    try:
        expected_prev_hash = "0" * 64
        with open(audit_file, encoding="utf-8") as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)

                # Check link to previous entry
                actual_prev_hash = entry.get("prev_hash")
                if actual_prev_hash != expected_prev_hash:
                    return False

                # Re-compute hash of current entry
                signature = entry.pop("hash", None)
                if signature is None:
                    return False

                canonical_str = json.dumps(entry, sort_keys=True, separators=(",", ":"))
                computed_hash = get_sha256(canonical_str.encode("utf-8"))

                if computed_hash != signature:
                    return False

                expected_prev_hash = signature

        return True
    except Exception:
        return False


def log_event(ctx, category: str, detail_a: str, detail_b: str = "", detail_c: Any = None) -> None:
    """Route a structured event to the correct legacy table.

    Used by bridges that predate the unified memory_items model.
    Categories: 'thought'/'activity' → activity_logs; 'decision' → project_decisions.
    Unknown categories fall through to activity_logs for safety.
    """
    # Through the seam, so the events land in the live store on every backend
    # (PostgreSQL carries both tables since pg_052); it used the SQLite pool,
    # which writes a stale local file on any other backend.
    from m3_core.paths import active_database, seam_backend, seam_dialect

    now = utc_iso()
    marks = seam_dialect().placeholder(4)
    with active_database(ctx.db_path), seam_backend().connection() as conn:
        if category == "decision":
            conn.execute(
                f"INSERT INTO project_decisions (project, decision, rationale, timestamp) VALUES ({marks})",
                (detail_c or "default", detail_a, detail_b, now),
            )
        else:
            conn.execute(
                f"INSERT INTO activity_logs (timestamp, query, response, model_used) VALUES ({marks})",
                (now, detail_a, detail_b, detail_c or category),
            )
        conn.commit()
