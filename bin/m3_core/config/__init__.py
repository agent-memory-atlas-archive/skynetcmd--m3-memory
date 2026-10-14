"""m3's configuration: the registry, the m3_config.toml loader, the resolver.

Only
this package reads m3-owned environment variables once the cutover lands (N+3);
until then legacy readers keep working beside it (2D converts them).
"""

from __future__ import annotations

from .keys import REGISTRY
from .loader import (
    CONFIG_FILE_NAME,
    ConfigFileError,
    ConfigKeyError,
    ConfigSyntaxError,
    LoadedFile,
    Location,
    format_config_error,
    load,
    locate,
)
from .registry import ConfigValue, ConfigValueError, Key, KeyClass, Registry, Scope, parse_bool

__all__ = [
    "CONFIG_FILE_NAME",
    "REGISTRY",
    "ConfigFileError",
    "ConfigKeyError",
    "ConfigSyntaxError",
    "ConfigValue",
    "ConfigValueError",
    "Key",
    "KeyClass",
    "LoadedFile",
    "Location",
    "Registry",
    "Scope",
    "format_config_error",
    "load",
    "locate",
    "parse_bool",
]
