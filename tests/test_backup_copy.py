"""m3_core.atomic_io.GuardedRewrite: the one owner of m3's backups (user design,
2026-10-12). <name>.bak is the active backup (byte-exact, restore by copying it
back); <name>.backup_history gets one UTC-stamped section per rewrite, BEGIN
before the write and END after it, so a crash mid-rewrite is visible and says
how to restore."""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from m3_core import atomic_io  # noqa: E402
from m3_core.atomic_io import GuardedRewrite, backup_paths  # noqa: E402

_UTC = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


def test_a_rewrite_leaves_an_exact_backup_and_a_closed_history_section(tmp_path):
    f = tmp_path / "settings.json"
    f.write_bytes(b'{"a": 1}\n')
    GuardedRewrite(f, by="m3 doctor --fix").write('{"a": 2}\n')
    bak, hist = backup_paths(f)
    assert bak.name == "settings.json.bak" and hist.name == "settings.json.backup_history"
    assert bak.read_bytes() == b'{"a": 1}\n'
    json.loads(bak.read_text(encoding="utf-8"))  # still valid: restore is a plain copy back
    assert f.read_bytes() == b'{"a": 2}\n'
    text = hist.read_text(encoding="utf-8")
    assert re.search(rf"=== {_UTC} BEGIN rewrite of .*settings\.json by m3 doctor --fix ===", text), text
    assert '-{"a": 1}' in text and '+{"a": 2}' in text
    assert re.search(rf"=== {_UTC} END rewrite \(ok\) ===\n$", text), text


def test_history_keeps_every_change_and_bak_is_the_latest(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\n", encoding="utf-8")
    GuardedRewrite(f, by="t").write("A=2\n")
    GuardedRewrite(f, by="t").write("A=3\n")
    bak, hist = backup_paths(f)
    assert bak.read_text(encoding="utf-8") == "A=2\n"
    text = hist.read_text(encoding="utf-8")
    assert text.count(" BEGIN rewrite ") == 2 and text.count(" END rewrite (ok) ") == 2
    assert "-A=1" in text and "+A=3" in text


def test_a_crash_mid_rewrite_leaves_an_open_section_that_says_how_to_restore(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    f.write_text('{"a": 1}', encoding="utf-8")
    real = atomic_io.atomic_write

    def crash(path, data, **kw):
        if Path(path) == f:
            raise OSError("disk went away")
        return real(path, data, **kw)

    monkeypatch.setattr(atomic_io, "atomic_write", crash)
    with pytest.raises(OSError):
        GuardedRewrite(f, by="t").write('{"a": 2}')
    bak, hist = backup_paths(f)
    text = hist.read_text(encoding="utf-8")
    assert " BEGIN rewrite " in text and " END rewrite " not in text
    assert f"restore by copying {bak.name} over {f.name}" in text
    assert bak.read_text(encoding="utf-8") == '{"a": 1}'


@pytest.mark.requires_posix  # POSIX permission bits
def test_backup_and_history_keep_a_user_only_mode(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text("{}", encoding="utf-8")
    f.chmod(0o600)
    GuardedRewrite(f, by="t").write("{ }")
    for p in backup_paths(f):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p


def test_a_missing_original_raises_before_anything_is_written(tmp_path):
    f = tmp_path / "absent.json"
    with pytest.raises(OSError):
        GuardedRewrite(f, by="t").write("{}")
    assert not any(p.exists() for p in (f, *backup_paths(f)))


def test_content_that_is_not_a_file_is_backed_up_too(tmp_path):
    """A crontab: begin() takes the current content, end() the new."""
    g = GuardedRewrite(tmp_path / "crontab", by="m3 schedules")
    g.begin(b"* * * * * old\n")
    g.end(b"* * * * * new\n")
    bak, hist = backup_paths(tmp_path / "crontab")
    assert bak.read_bytes() == b"* * * * * old\n" and "+* * * * * new" in hist.read_text(encoding="utf-8")
