"""m3's configuration: the registry, the m3_config.toml loader, the resolver.

Settings come from m3_config.toml (or a --set flag) and the default; no
setting is read from the environment. The few m3 variables still read are
locators and bootstrap switches (config.keys NOT_SETTINGS, M3_CONFIG_ROOT).
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
