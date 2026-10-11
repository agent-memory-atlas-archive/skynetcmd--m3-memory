"""Root-1: move the legacy memory root (~/.m3-memory) into ~/.m3, then flip the default.

The ONE implementation: ``bin/homecoming.py``, ``m3 doctor --fix`` and ``m3 setup``
all call it. It lives in the package, not in ``bin/``, because the installer and
doctor run from an installed package where ``bin/`` is not importable. Like
``_roots`` it imports the standard library only.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from m3_memory import _roots as roots

logger = logging.getLogger("m3.root_migration")


def pending() -> bool:
    """True when there is legacy state to migrate: the legacy root exists, the
    marker does not, and no ``M3_MEMORY_ROOT`` pin makes the question moot."""
    return (
        not roots.memory_root_pin() and not roots.memory_root_migrated() and os.path.isdir(roots.legacy_memory_root())
    )


def migrate_memory_root(old_root=None, new_root=None, config_root=None):
    """Root-1 (user decision 2026-10-11, migrate-then-flip): move the legacy memory
    root's live state (~/.m3-memory) into ~/.m3, then write the marker that makes
    ``m3_memory._roots.memory_root()`` default to ~/.m3.

    COPIES, never moves or deletes: the old root keeps everything and gets a
    MOVED.txt. Idempotent: a second run, or one after the marker exists, does
    nothing. Skipped when M3_MEMORY_ROOT pins a root explicitly (the user chose it).

      audit_trail.log.jsonl   appended into the new file (old lines first)
      config.json             copied unless the new root already has one
      version_state.json, .env, backups/   copied unless present
      .agent_os_salt          copied to the CONFIG root if it lacks one: the vault
                              cannot be decrypted without it
      repo/                   left where it is (config.json keeps pointing at it)

    Returns a report dict; ``failed`` lists what could not be copied, and then the
    marker is NOT written, so the default does not flip on a partial migration.
    """
    report = {"moved": [], "kept": [], "failed": [], "skipped": None}
    if roots.memory_root_pin():
        report["skipped"] = "M3_MEMORY_ROOT pins the memory root explicitly"
        return report
    old = Path(old_root or roots.legacy_memory_root())
    new = Path(new_root or Path(os.path.expanduser("~")) / ".m3")
    cfg = Path(config_root or roots.config_root())
    marker = new / roots.MIGRATED_MARKER
    if os.path.exists(marker):
        report["skipped"] = "already migrated"
        return report
    new.mkdir(parents=True, exist_ok=True)

    def _copy(name, dst_dir=new):
        src, dst = old / name, dst_dir / name
        if not os.path.exists(src):
            return
        if os.path.exists(dst):
            report["kept"].append(str(dst))
            return
        try:
            shutil.copytree(src, dst) if os.path.isdir(src) else shutil.copy2(src, dst)
            report["moved"].append(name)
        except Exception as e:  # noqa: BLE001 — reported; the marker is then withheld
            report["failed"].append(f"{name}: {e}")

    if os.path.isdir(old):
        trail_src, trail_dst = old / "audit_trail.log.jsonl", new / "audit_trail.log.jsonl"
        if os.path.exists(trail_src):
            try:
                merged = trail_src.read_bytes() + (trail_dst.read_bytes() if os.path.exists(trail_dst) else b"")
                trail_dst.write_bytes(merged)
                report["moved"].append("audit_trail.log.jsonl (merged)")
            except Exception as e:  # noqa: BLE001
                report["failed"].append(f"audit_trail.log.jsonl: {e}")
        for name in ("config.json", "version_state.json", ".env", "backups"):
            _copy(name)
        cfg.mkdir(parents=True, exist_ok=True)
        _copy(".agent_os_salt", dst_dir=cfg)
        if not report["failed"]:
            (old / "MOVED.txt").write_text(
                f"m3's memory root moved to {new} (root-1). This directory was copied, not "
                f"deleted: remove it once m3 runs from the new root. repo/ is still used here.\n",
                encoding="utf-8",
            )
    if report["failed"]:
        logger.error("Memory-root migration INCOMPLETE, default NOT switched: " + "; ".join(report["failed"]))
        return report
    marker.write_text(f"migrated from {old}\n", encoding="utf-8")
    return report
