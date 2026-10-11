"""The installed package's route to the config registry (plan 2D.2 batch (b)).

`m3_core.config` lives in the payload's `bin/`, which the package reaches only
through `sys.path`. These delegate after `m3_memory._bin.ensure_on_path()`, so a
reader in `m3_memory/` writes ``_config.setting("embed.gguf")`` exactly as a
`bin/` reader writes ``settings.setting("embed.gguf")``: one accessor, one owner
(`m3_core.config.settings`), no per-site path handling.
"""

from __future__ import annotations

from typing import Any

from m3_memory import _bin


def _settings():
    _bin.ensure_on_path()
    from m3_core.config import settings

    return settings


def setting(name: str) -> Any:
    """Key ``name``'s current value (m3_config.toml > env names > default)."""
    return _settings().setting(name)


def explicit(name: str) -> bool:
    """True when key ``name`` was set by the file or an env name, not defaulted."""
    return _settings().explicit(name)
