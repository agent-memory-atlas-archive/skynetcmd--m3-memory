"""Resolve each config key to (value, origin) (CONFIG_DESIGN §2, §13).

Precedence for a CONFIG key, N..N+2 (the deprecation window, #D28):

    env, for an ``env_override`` key only (U9: db.backend)
    > m3_config.toml
    > legacy env (any other key's old env name; honoured, and reported, until N+3)
    > the registry default

So ``m3 config set`` wins at once even while HKCU / rc files / MCP env blocks
still hold an old value; the shadowed env name is reported, never silently
dropped. At N+3 the third rung goes. Secrets are not resolved here: their
chain (``m3_core.secret_store``) touches the keyring, so they resolve on demand.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from .loader import LoadedFile, show_key
from .registry import ConfigValue, ConfigValueError, Key, KeyClass, Registry

logger = logging.getLogger(__name__)

ORIGIN_DEFAULT = "default"


@dataclass(frozen=True, slots=True, kw_only=True)
class Resolved:
    value: ConfigValue | None
    origin: str                      # "env:NAME" | "file:<path> [t].k" | "env-legacy:NAME" | "default"
    shadowed: tuple[str, ...] = ()   # env names set but outranked by the file


class ConfigEnvError(ValueError):
    """An environment value that does not parse or validate for its key."""


class ConfigEnvErrors(ExceptionGroup):
    def derive(self, excs):
        return ConfigEnvErrors(self.message, excs)


def _env_hit(key: Key, environ: Mapping[str, str]) -> tuple[str, str] | None:
    for name in key.env_names:
        raw = environ.get(name)
        if raw is not None and raw.strip():
            return name, raw
    return None


def _from_env(key: Key, name: str, raw: str) -> ConfigValue:
    try:
        return key.coerce_env_value(raw)
    except ConfigValueError as e:
        # The value is shown: config, never a secret (secrets resolve elsewhere).
        err = ConfigEnvError(f"environment {name}={raw!r} (setting {show_key(key.name)}): {e}")
        err.add_note(f"fix or unset {name}; inspect: `m3 config show --origin`")
        raise err from None


_LEGACY_NOTED: set[tuple[str, str]] = set()


def _note_legacy_difference(key: Key, name: str, raw: str, value: ConfigValue) -> None:
    """§2 (#P11): where today's reader parsed a value differently (``== "1"``,
    ``!= "0"``), the canonical reading wins and the difference is logged ONCE per
    (name, value), so the user sees exactly which env value changed meaning."""
    if key.legacy_parse is None or (name, raw) in _LEGACY_NOTED:
        return
    try:
        old = key.legacy_parse(raw)
    except Exception:  # noqa: BLE001 -- the legacy reader would have failed too
        return
    if old != value:
        _LEGACY_NOTED.add((name, raw))
        logger.warning("m3 config: %s=%r now reads as %r for %s (it read as %r before release N; "
                       "set it in m3_config.toml or use true/false)", name, raw, value,
                       show_key(key.name), old)


def resolve_key(key: Key, loaded: LoadedFile, environ: Mapping[str, str]) -> Resolved:
    if key.klass is not KeyClass.CONFIG:
        raise ValueError(f"{key.name} is a {key.klass}; resolve secrets with m3_core.secret_store")
    hit = _env_hit(key, environ)
    if key.env_override and hit is not None:
        return Resolved(value=_from_env(key, *hit), origin=f"env:{hit[0]}")
    if key.name in loaded.values:
        shadowed = tuple(n for n in key.env_names if (environ.get(n) or "").strip())
        return Resolved(value=loaded.values[key.name],
                        origin=f"file:{loaded.location.file} {show_key(key.name)}", shadowed=shadowed)
    if hit is not None:
        value = _from_env(key, *hit)
        _note_legacy_difference(key, *hit, value)
        return Resolved(value=value, origin=f"env-legacy:{hit[0]}")
    # A list default is copied: the registry's own object must never be shared
    # with a caller that might mutate it.
    default = list(key.default) if isinstance(key.default, list) else key.default
    return Resolved(value=default, origin=ORIGIN_DEFAULT)


def resolve_all(registry: Registry, loaded: LoadedFile,
                environ: Mapping[str, str]) -> dict[str, Resolved]:
    """Every CONFIG key, or ``ConfigEnvErrors`` naming EVERY bad env value."""
    out: dict[str, Resolved] = {}
    errors: list[Exception] = []
    for key in registry:
        if key.klass is not KeyClass.CONFIG:
            continue
        try:
            out[key.name] = resolve_key(key, loaded, environ)
        except ConfigEnvError as e:
            errors.append(e)
    if errors:
        raise ConfigEnvErrors(f"{len(errors)} environment value(s) are invalid", errors)
    return out
