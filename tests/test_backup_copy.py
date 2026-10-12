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


@pytest.fixture(autouse=True)
def _config_root_exists():
    """A real install has its config root; the index lives there."""
    from m3_memory import _roots

    Path(_roots.config_root()).mkdir(parents=True, exist_ok=True)


_UTC = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z"


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
    assert f"copy {bak.name} over {f.name}" in text and f"m3 restore {f}" in text
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


def test_begin_records_the_file_in_the_index_once(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text("{}", encoding="utf-8")
    GuardedRewrite(f, by="t").write("{ }")
    GuardedRewrite(f, by="t").write("{  }")
    assert atomic_io.rewritten_files() == [f]


def test_an_interrupted_rewrite_is_found_and_a_finished_one_is_not(tmp_path, monkeypatch):
    done, crashed, undone = (tmp_path / n for n in ("done.json", "crashed.json", "undone.json"))
    for f in (done, crashed, undone):
        f.write_text("{}", encoding="utf-8")
    GuardedRewrite(done, by="t").write("{ }")
    GuardedRewrite(crashed, by="m3 doctor --fix").begin()  # the process died here
    g = GuardedRewrite(undone, by="t")
    g.begin()
    g.abort("test")
    found = atomic_io.unfinished_rewrites()
    assert [(p, by) for p, by, _ in found] == [(crashed, "m3 doctor --fix")]
    assert found[0][2].endswith("Z"), "the UTC time the rewrite began"


def test_a_crash_after_a_finished_one_is_still_found(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text("{}", encoding="utf-8")
    GuardedRewrite(f, by="first").write("{ }")
    GuardedRewrite(f, by="second").begin()
    assert [(p, by) for p, by, _ in atomic_io.unfinished_rewrites()] == [(f, "second")]


def test_doctor_names_the_file_and_the_restore_and_is_silent_when_clean(tmp_path, capsys):
    from m3_memory.install.sections import _interrupted_rewrite_section

    _interrupted_rewrite_section()
    assert capsys.readouterr().out == ""
    f = tmp_path / "settings.json"
    f.write_text("{}", encoding="utf-8")
    GuardedRewrite(f, by="m3 setup").begin()
    _interrupted_rewrite_section()
    out = capsys.readouterr().out
    assert str(f) in out and "settings.json.bak" in out and "m3 setup" in out


@pytest.mark.parametrize(
    "content",
    [
        b'{"a": 1}\n',
        b'{"a": 1}',
        b"",
        b"line one\r\nline two\r\n",
        b"line one\r\nline two",
        b"mixed\r\nendings\nhere\r\n",
        b"lone\rcr\n",
        b"\x00\xffbinary",
        b"=== looks like a header ===\n",
    ],
    ids=["lf", "lf-no-nl", "empty", "crlf", "crlf-no-nl", "mixed", "lone-cr", "binary", "header-lookalike"],
)
def test_every_version_restores_byte_for_byte(tmp_path, content):
    f = tmp_path / "settings.json"
    f.write_bytes(content)
    GuardedRewrite(f, by="t").write(b"new")
    [(stamp, by, saved)] = atomic_io.versions(f)
    assert (by, saved) == ("t", content)
    atomic_io.restore(f, stamp)
    assert f.read_bytes() == content


def test_the_history_quotes_saves_and_says_not_to_copy_it(tmp_path):
    f = tmp_path / "settings.json"
    f.write_bytes(b'{"a": 1}\n')
    GuardedRewrite(f, by="t").write('{"a": 2}\n')
    text = backup_paths(f)[1].read_text(encoding="utf-8")
    assert "This is a log: do NOT copy this file over settings.json." in text
    assert '| {"a": 1}' in text and "m3 restore" in text


def test_a_crlf_file_is_saved_readable_not_as_base64(tmp_path):
    """Windows editors write CRLF; its history must still be readable."""
    f = tmp_path / "settings.json"
    f.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')
    GuardedRewrite(f, by="t").write(b"{}")
    text = backup_paths(f)[1].read_text(encoding="utf-8")
    assert "CRLF line endings" in text and '|   "a": 1' in text and "base64" not in text


def test_an_older_version_comes_back_and_the_restore_can_be_undone(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text("v1\n", encoding="utf-8")
    GuardedRewrite(f, by="t").write("v2\n")
    GuardedRewrite(f, by="t").write("v3\n")
    first = atomic_io.versions(f)[0][0]
    atomic_io.restore(f, first)
    assert f.read_text(encoding="utf-8") == "v1\n"
    assert atomic_io.versions(f)[-1][2] == b"v3\n", "the restore saved what it replaced"
    with pytest.raises(KeyError):
        atomic_io.restore(f, "1999-01-01T00:00:00Z")


def test_m3_restore_lists_and_restores(tmp_path, capsys, monkeypatch):
    from m3_memory import cli

    f = tmp_path / "settings.json"
    f.write_text("v1\n", encoding="utf-8")
    GuardedRewrite(f, by="m3 setup").write("v2\n")
    monkeypatch.setattr(sys, "argv", ["m3", "restore", str(f)])
    with pytest.raises(SystemExit) as done:
        cli.main()
    out = capsys.readouterr().out
    assert done.value.code == 0 and "before a change by m3 setup" in out
    stamp = atomic_io.versions(f)[0][0]
    monkeypatch.setattr(sys, "argv", ["m3", "restore", str(f), "--at", stamp])
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == 0 and f.read_text(encoding="utf-8") == "v1\n"


def test_a_missing_config_root_is_warned_about_and_never_created(tmp_path, monkeypatch, caplog):
    import shutil

    from m3_memory import _roots

    root = Path(_roots.config_root())
    shutil.rmtree(root)
    f = tmp_path / "settings.json"
    f.write_text("{}", encoding="utf-8")
    GuardedRewrite(f, by="t").write("{ }")
    assert not root.exists(), "a root that does not exist is not this writer's to make"
    assert "does not exist" in caplog.text and f.with_name("settings.json.bak").exists()
