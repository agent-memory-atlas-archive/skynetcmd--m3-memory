import pathlib
import sys

sys.path.insert(0, str(pathlib.Path("bin").resolve()))
from m3_halt import _WRITER_CMDLINE_SIGNATURES, NON_BLOCKING_ROLES


def test_waiter_is_visible_but_non_blocking():
    assert "waiter" in _WRITER_CMDLINE_SIGNATURES, "Waiter must be visible to cmdline scan"
    assert "waiter" in NON_BLOCKING_ROLES, "Waiter must NOT block exclusive ops (it holds no store)"


def test_the_dispatch_binaries_are_found_and_never_block():
    """A running m3-dispatchd or m3-wait locks its .exe on Windows, so `m3 stop`
    must find them for an upgrade; neither holds a store."""
    from m3_halt import writer_role

    assert writer_role([r"C:\venv\Lib\site-packages\m3_core_rs\m3-dispatchd.exe", "--endpoint", "x"]) == "dispatchd"
    assert writer_role(["/venv/m3_core_rs/m3-wait", "--endpoint", "x"]) == "m3-wait"
    assert writer_role(["grep", "m3-dispatchd"]) is None, "a process merely naming it is not it"
    assert {"dispatchd", "m3-wait"} <= set(NON_BLOCKING_ROLES)
