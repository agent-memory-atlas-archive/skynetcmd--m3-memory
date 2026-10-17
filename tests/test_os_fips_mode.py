"""_platform.os_fips_mode reads the OS's own FIPS switch.

The Windows branch reads the FipsAlgorithmPolicy registry value through the
module's `_winreg` handle; a fake handle lets it run on every OS.
"""

from __future__ import annotations

import contextlib

from conftest import simulate_os
from m3_memory import _platform


class _FakeRegistry:
    HKEY_LOCAL_MACHINE = object()

    def __init__(self, value=None, missing=False):
        self._value = value
        self._missing = missing
        self.opened = []

    def OpenKey(self, root, path):  # noqa: N802 -- mirrors winreg
        if self._missing:
            raise FileNotFoundError(path)
        self.opened.append((root, path))
        return contextlib.nullcontext(object())

    def QueryValueEx(self, key, name):  # noqa: N802 -- mirrors winreg
        return self._value, 4


def test_windows_reads_the_policy_value(monkeypatch):
    simulate_os(monkeypatch, "Windows")
    for value, expect in ((1, True), (0, False)):
        reg = _FakeRegistry(value)
        monkeypatch.setattr(_platform, "_winreg", reg)
        assert _platform.os_fips_mode() is expect
        assert reg.opened and reg.opened[0][1].endswith(r"Lsa\FipsAlgorithmPolicy")


def test_windows_without_the_value_is_unknown(monkeypatch):
    simulate_os(monkeypatch, "Windows")
    monkeypatch.setattr(_platform, "_winreg", _FakeRegistry(missing=True))
    assert _platform.os_fips_mode() is None
    monkeypatch.setattr(_platform, "_winreg", None)
    assert _platform.os_fips_mode() is None
