"""The backend names are stated twice: `BackendName` (the type the backends are
checked against) and `SELECTABLE_BACKENDS` (the runtime tuple, kept in m3_core so
tools that must not import `memory` can share it). They must list the same names,
in the same order, or a backend becomes selectable without being typed (or the
reverse)."""

from __future__ import annotations

import importlib
from typing import get_args


def test_backend_name_literal_matches_the_selectable_tuple():
    base = importlib.import_module("memory.backends.base")
    paths = importlib.import_module("m3_core.paths")
    # `type BackendName = Literal[...]`: the Literal is the alias's __value__.
    assert get_args(base.BackendName.__value__) == paths.SELECTABLE_BACKENDS
