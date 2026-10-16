"""The installed package's route to the config registry.

`m3_core.config` lives in the payload's `bin/`, which the package reaches only
through `sys.path`. These delegate after `m3_memory._bin.ensure_on_path()`, so a
reader in `m3_memory/` writes ``_config.setting("embed.gguf")`` exactly as a
`bin/` reader writes ``settings.setting("embed.gguf")``: one accessor, one owner
(`m3_core.config.settings`), no per-site path handling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from m3_memory import _bin


def _settings():
    _bin.ensure_on_path()
    from m3_core.config import settings

    return settings


def _setting(name: str) -> Any:
    """Key ``name``'s current value (m3_config.toml > env names > default)."""
    return _settings().setting(name)


# Typed per key, like m3_core.config.settings.setting: the same generated overloads.
if TYPE_CHECKING:
    from m3_memory._setting_types import setting
else:
    setting = _setting


def explicit(name: str) -> bool:
    """True when key ``name`` was set by the file or an env name, not defaulted."""
    return bool(_settings().explicit(name))


def origin(name: str) -> str:
    """Where key ``name``'s value came from (``default``, ``file:...``, ``env-legacy:NAME``, ``cli``...)."""
    return str(_settings().origin_of(name))


def write(values: dict[str, Any]):
    """Set (None: remove) each named key in m3_config.toml, as `m3 config set`
    does (the one writer: m3_core.config.writer). Returns the file. Raises
    ConfigWriteError, naming the key, for a refused value; the file is then unchanged."""
    _bin.ensure_on_path()
    from m3_core.config import REGISTRY, locate
    from m3_core.config.writer import ConfigWriteError, set_values

    keys = {}
    for name, value in values.items():
        key = REGISTRY.get(name)
        if key is None:
            raise ConfigWriteError(f"{name} is not a registry key")
        keys[key] = value
    return set_values(locate(), keys)
