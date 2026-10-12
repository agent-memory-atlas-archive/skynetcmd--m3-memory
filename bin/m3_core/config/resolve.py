"""Resolve each config key to (value, origin) (CONFIG_DESIGN §2, §13).

Precedence for a CONFIG key, N..N+2 (the deprecation window, #D28):

    env, for an ``env_override`` key only (U9: db.backend)
    > m3_config.toml
    > legacy env (any other key's old env name; honoured, and reported, until N+3)
    > .env (parsed, never injected; CONFIG_DESIGN §10)
    > the registry default

So ``m3 config set`` wins at once even while HKCU / rc files / MCP env blocks
still hold an old value; the shadowed env name is reported, never silently
dropped. At N+3 the third rung goes. Secrets are not resolved here: their
chain (``m3_core.secret_store``) touches the keyring, so they resolve on demand.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast, override

from .loader import LoadedFile, show_key
from .registry import ConfigValue, ConfigValueError, Key, KeyClass, Registry

logger = logging.getLogger(__name__)

ORIGIN_DEFAULT = "default"


@dataclass(frozen=True, slots=True, kw_only=True)
class Resolved:
    value: ConfigValue | None
    origin: str  # "env:NAME" | "file:<path> [t].k" | "env-legacy:NAME" | "default"
    shadowed: tuple[str, ...] = ()  # env names set but outranked by the file
    env_name: str | None = None  # the env / .env name that supplied the value, if one did
    # For an env_override key answered by env: what the key resolves to WITHOUT
    # that env value (file, .env or default). ``setting()`` reads such a key's
    # env live and falls back to this when the variable is gone since the snapshot.
    beneath: Resolved | None = None


class ConfigEnvError(ValueError):
    """An environment value that does not parse or validate for its key."""


class ConfigEnvErrors(ExceptionGroup):
    @override
    def derive(self, excs):
        return ConfigEnvErrors(self.message, excs)


def _env_hit(key: Key, environ: Mapping[str, str]) -> tuple[str, str] | None:
    for name in key.env_names:
        raw = environ.get(name)
        if raw is not None and raw.strip():
            return name, raw
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class DotEnv:
    """The legacy .env, parsed, never injected (CONFIG_DESIGN §10)."""

    path: str | None = None
    values: Mapping[str, str] = field(default_factory=dict)


NO_DOTENV = DotEnv()


def from_env(key: Key, name: str, raw: str, where: str = "environment") -> ConfigValue:
    """``raw`` (env name ``name``) read by ``key``'s one env parser; raises
    ``ConfigEnvError`` naming the value and the fix."""
    try:
        return cast("ConfigValue", key.coerce_env_value(raw))
    except ConfigValueError as e:
        # The value is shown: config, never a secret (secrets resolve elsewhere).
        fix = f"{name} (fix or unset it)" if where == "environment" else f"{name} in {where} (fix or remove the line)"
        raise ConfigEnvError(
            f"observed: {where} {name}={raw!r} for setting {show_key(key.name)}: {e}. "
            f"inspect: {fix}; `m3 config show --origin`"
        ) from None


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
        logger.warning(
            "m3 config: %s=%r now reads as %r for %s (it read as %r before release N; "
            "set it in m3_config.toml or use true/false)",
            name,
            raw,
            value,
            show_key(key.name),
            old,
        )


def env_outranks_file(key: Key) -> bool:
    """True for an ``env_override`` key (U9: db.backend): its env value beats
    m3_config.toml. Every other key: the file wins (Option B)."""
    return key.env_override


def resolve_key(key: Key, loaded: LoadedFile, environ: Mapping[str, str], dotenv: DotEnv = NO_DOTENV) -> Resolved:
    if key.klass is not KeyClass.CONFIG:
        # A contract violation, not a diagnostic: secrets resolve in secret_store.
        raise ValueError(f"{key.name} is a {key.klass} key; secret_store resolves secrets, not resolve_key")
    hit = _env_hit(key, environ)
    if env_outranks_file(key) and hit is not None:
        without = {n: v for n, v in environ.items() if n not in key.env_names}
        return Resolved(
            value=from_env(key, *hit),
            origin=f"env:{hit[0]}",
            env_name=hit[0],
            beneath=_resolve_below_env(key, loaded, without, dotenv),
        )
    return _resolve_below_env(key, loaded, environ, dotenv)


def live_env_override(key: Key, resolved: Resolved, environ: Mapping[str, str]) -> ConfigValue | None:
    """An ``env_override`` key's value read from the LIVE environment: env when set
    now, else what the snapshot holds beneath a since-removed env value. The
    snapshot is invalidated by the file, not by env, and such a key was always
    re-read on every call (``paths.resolve_backend_name``)."""
    hit = _env_hit(key, environ)
    if hit is not None:
        return from_env(key, *hit)
    if resolved.beneath is not None:
        return resolved.beneath.value
    return resolved.value


def _resolve_below_env(key: Key, loaded: LoadedFile, environ: Mapping[str, str], dotenv: DotEnv) -> Resolved:
    hit = _env_hit(key, environ)
    dot = _env_hit(key, dotenv.values)
    if key.name in loaded.values:
        shadowed = tuple(n for n in key.env_names if (environ.get(n) or "").strip())
        if dot is not None:
            shadowed += (f"{dot[0]} (in {dotenv.path})",)
        return Resolved(
            value=loaded.values[key.name], origin=f"file:{loaded.location.file} {show_key(key.name)}", shadowed=shadowed
        )
    if hit is not None:
        value = from_env(key, *hit)
        _note_legacy_difference(key, *hit, value)
        shadowed = (f"{dot[0]} (in {dotenv.path})",) if dot is not None else ()
        return Resolved(value=value, origin=f"env-legacy:{hit[0]}", shadowed=shadowed, env_name=hit[0])
    if dot is not None:  # the lowest source: below the file and below real env
        value = from_env(key, *dot, where=f".env {dotenv.path}")
        _note_legacy_difference(key, *dot, value)
        return Resolved(value=value, origin=f"dotenv:{dotenv.path} {dot[0]}", env_name=dot[0])
    # A list default is copied: the registry's own object must never be shared
    # with a caller that might mutate it.
    default = list(key.default) if isinstance(key.default, list) else key.default
    return Resolved(value=default, origin=ORIGIN_DEFAULT)


def resolve_all(
    registry: Registry, loaded: LoadedFile, environ: Mapping[str, str], dotenv: DotEnv = NO_DOTENV
) -> dict[str, Resolved]:
    """Every CONFIG key, or ``ConfigEnvErrors`` naming EVERY bad env value."""
    out: dict[str, Resolved] = {}
    errors: list[Exception] = []
    for key in registry:
        if key.klass is not KeyClass.CONFIG:
            continue
        try:
            out[key.name] = resolve_key(key, loaded, environ, dotenv)
        except ConfigEnvError as e:
            errors.append(e)
    if errors:
        raise ConfigEnvErrors(f"{len(errors)} environment value(s) are invalid", errors)
    return out
