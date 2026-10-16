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

MOVED_NOTE = "MOVED.txt"  # left in the old root after its state was copied out


def moved_away(root: object) -> bool:
    """Whether ``root`` is a memory root whose state this migration copied
    elsewhere: it still exists (repo/ stays), so only its note tells."""
    return isinstance(root, str) and bool(root) and os.path.isfile(Path(root).expanduser() / MOVED_NOTE)


def pending() -> bool:
    """True when there is legacy state to migrate: the legacy root exists, the
    marker does not, and no ``M3_MEMORY_ROOT`` pin makes the question moot."""
    return (
        not roots.memory_root_pin() and not roots.memory_root_migrated() and os.path.isdir(roots.legacy_memory_root())
    )


def migrate_memory_root(old_root=None, new_root=None, config_root=None):
    """Root-1: move the legacy memory
    root's live state (~/.m3-memory) into ~/.m3, then write the marker that makes
    ``m3_memory._roots.memory_root()`` default to ~/.m3.

    COPIES, never moves or deletes: the old root keeps everything and gets a
    MOVED.txt. Idempotent: a second run, or one after the marker exists, does
    nothing. Skipped when M3_MEMORY_ROOT pins a root explicitly (the user chose it).

      audit_trail.log.jsonl   appended into the new file (old lines first)
      config.json             copied unless the new root already has one
      version_state.json, .env              copied unless present
      backups/                left where it is: nothing reads it (backups go to
                              <engine root>/backups), and copying it doubled
                              74 GB on a real box (2026-10-11)
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
        # A failed later step withholds the migration marker, so this runs again
        # on retry; its own marker keeps the old trail from being prepended twice.
        trail_merged = new / ".audit_trail_merged"
        if os.path.exists(trail_src) and not os.path.exists(trail_merged):
            try:
                merged = trail_src.read_bytes() + (trail_dst.read_bytes() if os.path.exists(trail_dst) else b"")
                trail_dst.write_bytes(merged)
                trail_merged.write_text(f"merged from {trail_src}\n", encoding="utf-8")
                report["moved"].append("audit_trail.log.jsonl (merged)")
            except Exception as e:  # noqa: BLE001
                report["failed"].append(f"audit_trail.log.jsonl: {e}")
        for name in ("config.json", "version_state.json", ".env"):
            _copy(name)
        cfg.mkdir(parents=True, exist_ok=True)
        _copy(".agent_os_salt", dst_dir=cfg)
        if not report["failed"]:
            (old / MOVED_NOTE).write_text(
                f"m3's memory root is now {new} (root-1). Its state files were COPIED there; "
                f"the copies here are no longer read. Keep repo/ in this directory: it is still used. "
                f"Everything else here may be deleted once m3 runs from the new root.\n",
                encoding="utf-8",
            )
    if report["failed"]:
        logger.error("Memory-root migration INCOMPLETE, default NOT switched: " + "; ".join(report["failed"]))
        return report
    marker.write_text(f"migrated from {old}\n", encoding="utf-8")
    return report
