"""Root-1: the memory root moves from ~/.m3-memory to ~/.m3, migrate-then-flip
(user decision 2026-10-11). The default flips only after the migration ran, the
migration copies and never deletes, and a second run changes nothing."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh home with a planted legacy ~/.m3-memory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # the home on Windows
    monkeypatch.delenv("M3_MEMORY_ROOT", raising=False)  # the sandbox pins it; Root-1 needs it unpinned
    old = tmp_path / ".m3-memory"
    (old / "backups").mkdir(parents=True)
    (old / "audit_trail.log.jsonl").write_text('{"old": 1}\n', encoding="utf-8")
    (old / "config.json").write_text('{"repo_path": "x"}', encoding="utf-8")
    (old / "version_state.json").write_text("{}", encoding="utf-8")
    (old / ".agent_os_salt").write_bytes(b"salt")
    (old / "backups" / "b1.db").write_bytes(b"db")
    return tmp_path


@pytest.fixture
def pinned_root(home, monkeypatch):
    monkeypatch.setenv("M3_MEMORY_ROOT", str(home / "custom"))
    return home / "custom"


def _migrate(home):
    import homecoming

    return homecoming.migrate_memory_root(config_root=home / ".m3" / "config")


def test_the_default_flips_only_after_migration(home):
    from m3_memory import _roots

    assert Path(_roots.memory_root()) == home / ".m3-memory", "an un-migrated install must keep its old root"
    report = _migrate(home)
    assert not report["failed"], report
    assert Path(_roots.memory_root()) == home / ".m3"


def test_state_is_copied_the_old_root_is_kept(home):
    _migrate(home)
    new, old = home / ".m3", home / ".m3-memory"
    assert (new / "config.json").read_text(encoding="utf-8") == '{"repo_path": "x"}'
    assert (new / "backups" / "b1.db").read_bytes() == b"db"
    assert (home / ".m3" / "config" / ".agent_os_salt").read_bytes() == b"salt", (
        "the vault salt must reach the config root"
    )
    assert (old / "config.json").exists() and (old / "MOVED.txt").exists(), "the old root is kept, with a note"


def test_the_audit_trail_is_merged_not_overwritten(home):
    new = home / ".m3"
    new.mkdir()
    (new / "audit_trail.log.jsonl").write_text('{"new": 1}\n', encoding="utf-8")
    _migrate(home)
    assert (new / "audit_trail.log.jsonl").read_text(encoding="utf-8") == '{"old": 1}\n{"new": 1}\n'


def test_a_second_run_changes_nothing(home):
    _migrate(home)
    trail = (home / ".m3" / "audit_trail.log.jsonl").read_text(encoding="utf-8")
    assert _migrate(home)["skipped"] == "already migrated"
    assert (home / ".m3" / "audit_trail.log.jsonl").read_text(encoding="utf-8") == trail, "the trail was merged twice"


def test_a_pinned_memory_root_is_left_alone(home, pinned_root):
    assert _migrate(home)["skipped"]
    assert not (home / ".m3" / ".memory_root_migrated").exists()


def test_legacy_lookups_still_find_old_state_after_the_flip(home):
    """The flip moves memory_root() to ~/.m3, but a pre-Homecoming database still
    in ~/.m3-memory/memory/ must keep resolving: legacy fallbacks ask the legacy
    root, never memory_root()."""
    from m3_core import paths

    legacy_db = home / ".m3-memory" / "memory" / "files_database.db"
    legacy_db.parent.mkdir()
    legacy_db.write_bytes(b"x")
    _migrate(home)
    assert Path(paths.get_m3_root()) == home / ".m3"
    assert Path(paths.resolve_engine_file("files_database.db")) == legacy_db


def test_a_pin_is_the_legacy_root_too(pinned_root):
    from m3_memory import _roots

    assert Path(_roots.legacy_memory_root()) == pinned_root
    assert Path(_roots.memory_root()) == pinned_root


def test_one_implementation(home):
    import homecoming

    from m3_memory import _root_migration

    assert homecoming.migrate_memory_root is _root_migration.migrate_memory_root


def test_doctor_reports_then_fixes(home, capsys):
    from m3_memory import _root_migration, installer

    assert _root_migration.pending()
    installer._memory_root_migration_section(fix=False)
    assert "doctor --fix" in capsys.readouterr().out
    assert _root_migration.pending(), "a report must not migrate"
    installer._memory_root_migration_section(fix=True)
    assert "moved to ~/.m3" in capsys.readouterr().out
    assert not _root_migration.pending()
    installer._memory_root_migration_section(fix=False)
    assert capsys.readouterr().out == "", "nothing to migrate must print nothing"


def test_setup_step_migrates(home):
    from m3_memory import _roots, setup_wizard

    assert setup_wizard._step_migrate_memory_root()
    assert _roots.memory_root_migrated()


def test_a_failed_copy_withholds_the_flip(home, monkeypatch):
    import shutil

    from m3_memory import _roots

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", boom)
    report = _migrate(home)
    assert report["failed"]
    assert not _roots.memory_root_migrated(), "a partial migration must not switch the default"
    assert Path(_roots.memory_root()) == home / ".m3-memory"
    assert os.path.exists(home / ".m3-memory" / "config.json")
