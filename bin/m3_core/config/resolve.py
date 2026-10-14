"""Resolve each config key to (value, origin) (CONFIG_DESIGN §2, §13).

Precedence for a CONFIG key, N..N+2 (the deprecation window, #D28):

    m3_config.toml (no key's env value outranks it, db.backend
    included)
    > legacy env (any other key's old env name; honoured, and reported, until N+3)
    > .env (parsed, never injected; CONFIG_DESIGN §10)
    > <config_root>/.llm_config.json (the [llm] keys' old file; folds in at import-env)
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
    """A legacy file of env names, parsed, never injected: the .env (CONFIG_DESIGN
    §10) or, below it, ``.llm_config.json`` (kind "llmfile"; fold plan
    2026.10.12). ``kind`` is the origin's prefix."""

    path: str | None = None
    values: Mapping[str, str] = field(default_factory=dict)
    kind: str = "dotenv"

    @property
    def where(self) -> str:
        return f".env {self.path}" if self.kind == "dotenv" else str(self.path)


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


def resolve_key(
    key: Key,
    loaded: LoadedFile,
    environ: Mapping[str, str],
    dotenv: DotEnv = NO_DOTENV,
    llmfile: DotEnv = NO_DOTENV,
) -> Resolved:
    if key.klass is not KeyClass.CONFIG:
        # A contract violation, not a diagnostic: secrets resolve in secret_store.
        raise ValueError(f"{key.name} is a {key.klass} key; secret_store resolves secrets, not resolve_key")
    return _resolve_below_env(key, loaded, environ, (dotenv, llmfile))


def _resolve_below_env(
    key: Key, loaded: LoadedFile, environ: Mapping[str, str], legacy: tuple[DotEnv, ...]
) -> Resolved:
    hit = _env_hit(key, environ)
    # The legacy files that set this key, highest first (.env, then the LLM file).
    files = [(src, h) for src in legacy if (h := _env_hit(key, src.values)) is not None]

    def unused(below: list[tuple[DotEnv, tuple[str, str]]]) -> tuple[str, ...]:
        return tuple(f"{h[0]} (in {src.path})" for src, h in below)

    if key.name in loaded.values:
        shadowed = tuple(n for n in key.env_names if (environ.get(n) or "").strip()) + unused(files)
        return Resolved(
            value=loaded.values[key.name], origin=f"file:{loaded.location.file} {show_key(key.name)}", shadowed=shadowed
        )
    if hit is not None:
        value = from_env(key, *hit)
        _note_legacy_difference(key, *hit, value)
        return Resolved(value=value, origin=f"env-legacy:{hit[0]}", shadowed=unused(files), env_name=hit[0])
    if files:  # the lowest sources: below the file and below real env
        (src, dot), below = files[0], files[1:]
        value = from_env(key, *dot, where=src.where)
        _note_legacy_difference(key, *dot, value)
        return Resolved(value=value, origin=f"{src.kind}:{src.path} {dot[0]}", shadowed=unused(below), env_name=dot[0])
    # A list default is copied: the registry's own object must never be shared
    # with a caller that might mutate it.
    default = list(key.default) if isinstance(key.default, list) else key.default
    return Resolved(value=default, origin=ORIGIN_DEFAULT)


def resolve_all(
    registry: Registry,
    loaded: LoadedFile,
    environ: Mapping[str, str],
    dotenv: DotEnv = NO_DOTENV,
    llmfile: DotEnv = NO_DOTENV,
) -> dict[str, Resolved]:
    """Every CONFIG key, or ``ConfigEnvErrors`` naming EVERY bad env value."""
    out: dict[str, Resolved] = {}
    errors: list[Exception] = []
    for key in registry:
        if key.klass is not KeyClass.CONFIG:
            continue
        try:
            out[key.name] = resolve_key(key, loaded, environ, dotenv, llmfile)
        except ConfigEnvError as e:
            errors.append(e)
    if errors:
        raise ConfigEnvErrors(f"{len(errors)} environment value(s) are invalid", errors)
    return out
