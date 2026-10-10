"""The installer's tarball filter = m3's path checks + tarfile.data_filter.

_safe_tar_member DROPS traversal, outside links and special files (a GitHub
tarball may carry odd entries, so it drops rather than aborts). It never
touched permissions, so a setuid or world-writable entry came through as-is.
tarfile.data_filter (3.12) adds that half: it clears setuid/setgid/sticky and
group/other write, and resets ownership. The composed filter applies both, in
that order, so data_filter only ever sees entries m3 already accepted.
"""

from __future__ import annotations

import tarfile

import pytest


def _member(name, mode=0o644, kind=tarfile.REGTYPE, linkname=""):
    m = tarfile.TarInfo(name)
    m.type = kind
    m.mode = mode
    m.linkname = linkname
    m.uid, m.gid, m.uname, m.gname = 1234, 5678, "someone", "somegroup"
    return m


@pytest.fixture
def filt(tmp_path):
    from m3_memory.install.fs import _safe_tar_filter

    root = tmp_path.resolve()
    return _safe_tar_filter(root), str(root)


def test_setuid_and_group_world_write_are_cleared(filt):
    f, root = filt
    out = f(_member("m3-memory-x/bin/tool", mode=0o6777), root)
    assert out is not None
    assert out.mode & 0o7000 == 0, oct(out.mode)  # no setuid/setgid/sticky
    assert out.mode & 0o022 == 0, oct(out.mode)  # no group/other write
    assert out.uid is None and out.uname is None  # ownership not carried over


def test_regular_entry_kept(filt):
    f, root = filt
    out = f(_member("m3-memory-x/README.md"), root)
    assert out is not None and out.name == "m3-memory-x/README.md"


def test_real_extractall_round_trip(tmp_path):
    """extractall calls the filter as tarfile does: kept files land, the escape does not."""
    import io

    from m3_memory.install.fs import _safe_tar_filter

    archive = tmp_path / "t.tar"
    with tarfile.open(archive, "w") as tf:
        for name, data in (("m3-memory-x/ok.txt", b"ok"), ("../escape.txt", b"bad")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    dest = tmp_path / "out"
    dest.mkdir()
    with tarfile.open(archive) as tf:
        tf.extractall(dest, filter=_safe_tar_filter(dest))
    assert (dest / "m3-memory-x" / "ok.txt").read_bytes() == b"ok"
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize(
    "member",
    [
        _member("../escape.txt"),
        _member("/etc/passwd"),
        _member("dev", kind=tarfile.CHRTYPE),
        _member("link", kind=tarfile.SYMTYPE, linkname="/etc/passwd"),
    ],
)
def test_unsafe_entries_are_dropped_not_raised(filt, member):
    f, root = filt
    assert f(member, root) is None
