"""The watchdogs keep their own WMI-safe os_name() on purpose: a watchdog is the
repair path and must run when the m3 package is broken, so it cannot import the
OS seam. That makes the copies the seam's one documented exemption, and this
test holds them to it on every OS, so the copies cannot drift."""

from __future__ import annotations

import importlib
import os
import sys

import pytest

from m3_memory._platform import os_name as seam_os_name

_CASES = [("nt", "win32"), ("posix", "darwin"), ("posix", "linux"), ("posix", "freebsd14")]


@pytest.mark.parametrize("module", ["m3_embed_watchdog", "m3_loop_watchdog"])
def test_watchdog_os_name_matches_the_seam(module, monkeypatch):
    watchdog = importlib.import_module(module)
    for os_value, platform_value in _CASES:
        # Patched in the test body: conftest restores the real values per phase.
        monkeypatch.setattr(os, "name", os_value)
        monkeypatch.setattr(sys, "platform", platform_value)
        assert watchdog._os_name() == seam_os_name(), (module, os_value, platform_value)
