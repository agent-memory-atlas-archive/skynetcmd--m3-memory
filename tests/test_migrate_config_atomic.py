"""migrate_memory.save_config must not truncate .migrate_config.json on a failed write.

It wrote with open(path, "w"): the file is emptied first, so a crash or a
failed json.dump left a zero-byte or partial config and the user's remembered
backup_dir was lost. It now goes through m3_core.atomic_io.atomic_write
(plan Step 2A.3, user decision 2026-10-08).
"""

from __future__ import annotations

import json

import pytest


def test_failed_write_keeps_the_old_config(monkeypatch, tmp_path):
    import migrate_memory
    from m3_core import atomic_io

    path = tmp_path / ".migrate_config.json"
    monkeypatch.setattr(migrate_memory, "CONFIG_PATH", str(path))
    migrate_memory.save_config({"backup_dir": "/old/backups"})

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io.os, "replace", _boom)
    with pytest.raises(OSError):
        migrate_memory.save_config({"backup_dir": "/new/backups"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"backup_dir": "/old/backups"}


def test_round_trip(monkeypatch, tmp_path):
    import migrate_memory

    monkeypatch.setattr(migrate_memory, "CONFIG_PATH", str(tmp_path / "sub" / ".migrate_config.json"))
    migrate_memory.save_config({"backup_dir": "/b"})
    assert migrate_memory.load_config() == {"backup_dir": "/b"}
