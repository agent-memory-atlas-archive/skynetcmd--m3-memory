"""The settings snapshot and its reload (CONFIG_DESIGN §3, §7).

A ``Settings`` is frozen: a process (or a daemon's work cycle) reads one
consistent snapshot. ``current()`` hands out the process snapshot and reloads it
when the file's identity changes, checked at most every ``ttl_s`` seconds. A
reload that fails keeps the LAST GOOD snapshot and logs loudly (a crash would
restart-loop a scheduled daemon and take every tool out of the MCP stdio server);
``last_error()`` is what doctor reports. A ``reloadable=False`` key keeps its
startup value and logs "restart required".

Tests override with ``dataclasses.replace`` (``override()`` wraps it), never 3.13
``copy.replace``.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from m3_core.paths import StoreRef

from . import env as _env
from .keys import REGISTRY
from .loader import FileIdentity, LoadedFile, Location, file_identity, format_config_error, load, locate
from .registry import ConfigValue, Key, Registry
from .resolve import DotEnv, Resolved, resolve_all

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 5.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    location: Location
    identity: FileIdentity | None
    resolved: Mapping[str, Resolved]
    warnings: tuple[str, ...] = ()
    retired_env: tuple[str, ...] = ()  # retired names that are SET: they have no effect
    stores: Mapping[str, StoreRef] = field(default_factory=lambda: MappingProxyType({}))

    def __getitem__[T: ConfigValue](self, key: Key[T]) -> T:
        try:
            return self.resolved[key.name].value  # type: ignore[return-value]
        except KeyError:
            raise KeyError(f"{key.name} is not a config key of this snapshot") from None

    def origin(self, key: Key) -> str:
        return self.resolved[key.name].origin

    def legacy_env_in_use(self) -> list[str]:
        """Env names honoured only by the N..N+2 window (§13): what to migrate."""
        return sorted(
            r.origin.removeprefix("env-legacy:") for r in self.resolved.values() if r.origin.startswith("env-legacy:")
        )

    def deprecated_aliases_in_use(self) -> dict[str, str]:
        """{old name: canonical name} for every key whose value came from a
        legacy ALIAS spelling (env or .env), the registry's half of
        m3_core.paths.deprecated_env_in_use (`m3 doctor`'s migration list)."""
        out: dict[str, str] = {}
        for name, r in self.resolved.items():
            key = REGISTRY.get(name)
            if key is not None and r.env_name is not None and r.env_name != key.env_names[0]:
                out[r.env_name] = key.env_names[0]
        return out

    def shadowed_env(self) -> list[str]:
        """Env names set but outranked by m3_config.toml: ignored, and said so."""
        return sorted({n for r in self.resolved.values() for n in r.shadowed})


def build(
    location: Location | None = None,
    *,
    registry: Registry = REGISTRY,
    environ: Mapping[str, str] | None = None,
    dotenv: DotEnv | None = None,
) -> Settings:
    """Load and resolve a snapshot. Raises ``ConfigFileError`` / ``ConfigEnvErrors``."""
    from .keys import RETIRED

    loc = location if location is not None else locate()
    loaded: LoadedFile = load(loc, registry)
    env = environ if environ is not None else _env.environ()
    if dotenv is None:
        path, values = _env.dotenv() if environ is None else (None, {})
        dotenv = DotEnv(path=path, values=values)
    resolved = resolve_all(registry, loaded, env, dotenv)
    retired = tuple(sorted(n for n in RETIRED if (env.get(n) or "").strip()))
    return Settings(
        location=loc,
        identity=loaded.identity,
        resolved=MappingProxyType(resolved),
        warnings=loaded.warnings,
        retired_env=retired,
        stores=loaded.stores,
    )


def override(settings: Settings, values: Mapping[Key, ConfigValue]) -> Settings:
    """A copy with ``values`` replaced (origin ``override``). Tests use this."""
    resolved = dict(settings.resolved)
    for key, value in values.items():
        key.check(value)
        resolved[key.name] = Resolved(value=value, origin="override")
    return dataclasses.replace(settings, resolved=MappingProxyType(resolved))


_ALIAS_WARNED: set[str] = set()


def _warn_deprecated_aliases(settings: Settings) -> None:
    """Once per process per name, as getenv_compat did for the keys it served
    before they moved to the registry (plan 2D.1)."""
    for old, new in sorted(settings.deprecated_aliases_in_use().items()):
        if old not in _ALIAS_WARNED:
            _ALIAS_WARNED.add(old)
            logger.warning(
                "Deprecated env var %s is set; use %s instead (or m3_config.toml). The "
                "old name still works for now but will be removed. (`m3 doctor` lists "
                "all in use.)",
                old,
                new,
            )


class _Cache:
    """The process snapshot. One per process; guarded for threaded MCP tools."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings: Settings | None = None
        self._checked_at = 0.0
        self._last_error: str | None = None
        self._cli: dict[Key, ConfigValue] = {}
        self.ttl_s = DEFAULT_TTL_S

    def _with_cli(self, settings: Settings) -> Settings:
        if not self._cli:
            return settings
        resolved = dict(settings.resolved)
        for key, value in self._cli.items():
            resolved[key.name] = Resolved(value=value, origin="cli")
        return dataclasses.replace(settings, resolved=MappingProxyType(resolved))

    def set_cli(self, values: Mapping[Key, ConfigValue]) -> None:
        with self._lock:
            self._cli.update(values)
            if self._settings is not None:
                self._settings = self._with_cli(self._settings)

    def current(self) -> Settings:
        with self._lock:
            now = time.monotonic()
            if self._settings is None:
                self._settings = self._with_cli(build())  # startup: a bad file raises (§3)
                self._checked_at = now
                _warn_deprecated_aliases(self._settings)
                return self._settings
            if now - self._checked_at < self.ttl_s:
                return self._settings
            self._checked_at = now
            if file_identity(self._settings.location.file) == self._settings.identity:
                return self._settings
            self._settings = self._with_cli(self._reload(self._settings))
            return self._settings

    def _reload(self, old: Settings) -> Settings:
        try:
            new = build(old.location)
        except Exception as e:  # noqa: BLE001 -- keep the last good snapshot (§3)
            self._last_error = format_config_error(e)
            logger.error("m3 config reload failed; still using the settings loaded before.\n%s", self._last_error)
            return old
        self._last_error = None
        frozen = {
            k.name: old.resolved[k.name]
            for k in REGISTRY
            if not k.reloadable and k.name in old.resolved and new.resolved[k.name].value != old.resolved[k.name].value
        }
        for name in frozen:
            logger.warning(
                "m3 config: %s changed in %s; restart required (it is not reloadable)", name, old.location.file
            )
        if not frozen:
            return new
        return dataclasses.replace(new, resolved=MappingProxyType({**new.resolved, **frozen}))

    def last_error(self) -> str | None:
        return self._last_error

    def reset(self) -> None:
        with self._lock:
            self._settings = None
            self._checked_at = 0.0
            self._last_error = None
            self._cli = {}


_CACHE = _Cache()


def current() -> Settings:
    """The process snapshot, reloaded when the file changes (at most every TTL)."""
    return _CACHE.current()


def _setting(name: str) -> ConfigValue | None:
    """Key ``name``'s current value: m3_config.toml, then the key's env names, then
    its default, parsed by the key's one parser.

    The ONE accessor for code converted off direct env reads (plan 2D.2 batch (b)).
    A private ``_setting`` per module would be ~100 copies of these three lines.
    A name that is not a registry key raises, rather than reading a default
    nobody declared.
    """
    key = REGISTRY.get(name)
    if key is None:
        raise KeyError(f"observed: {name!r} is not a registry key. inspect: m3_core/config/keys*.py")
    value: ConfigValue | None = current()[key]
    return value


# Statically, ``setting`` has one overload per value type, generated from this
# registry (bin/gen_setting_types.py), so ``setting("search.row_cap")`` is an int.
# A single ``-> ConfigValue`` left every caller with the full union.
if TYPE_CHECKING:
    from m3_memory._setting_types import setting
else:
    setting = _setting


def set_from_cli(values: Mapping[str, ConfigValue | None]) -> None:
    """Command-line flags as settings for THIS process (origin ``cli``), above the
    file and env (R-explicit: an explicit flag beats env; env is only a default).

    What a CLI used to do with ``os.environ[NAME] = flag``: readers that call
    ``setting()`` see the value with no plumbing, it survives config reloads, and
    nothing leaks into the environment of unrelated children. A ``None`` value
    (flag not given) changes nothing. Validated like any value: a bad flag fails
    here, loud. A child that needs the value gets the flag, not env (2E.1)."""
    given: dict[Key, ConfigValue] = {}
    for name, value in values.items():
        if value is None:
            continue
        key = REGISTRY.get(name)
        if key is None:
            raise KeyError(f"observed: {name!r} is not a registry key. inspect: m3_core/config/keys*.py")
        if isinstance(value, str):
            value = key.parse(value)  # a flag's text, read by the key's one parser
        key.check(value)
        given[key] = value
    if given:
        _CACHE.set_cli(given)


def origin_of(name: str) -> str:
    """Where key ``name``'s value came from: ``default``, ``env:<NAME>``,
    ``env-legacy:<NAME>`` or the config file. The reporting path for diagnostics
    that ask "is this pinned in the process environment?" (CONFIG_DESIGN §2,
    ignored-env reporting): they read the origin, not os.environ."""
    key = REGISTRY.get(name)
    if key is None:
        raise KeyError(f"observed: {name!r} is not a registry key. inspect: m3_core/config/keys*.py")
    return current().origin(key)


def explicit(name: str) -> bool:
    """True when key ``name`` was SET (by m3_config.toml or an env name), False when
    it resolved to its default.

    For readers whose behaviour depends on whether the user chose a value, not
    just on the value: ``if os.environ.get("M3_EXTRACTION_TYPE"):`` meant "an
    extraction type was configured", and ``setting()`` alone cannot say that for a
    key with a default.
    """
    key = REGISTRY.get(name)
    if key is None:
        raise KeyError(f"observed: {name!r} is not a registry key. inspect: m3_core/config/keys*.py")
    return current().origin(key) != "default"


def last_error() -> str | None:
    """The most recent failed reload, for doctor; None when the last one succeeded."""
    return _CACHE.last_error()


def _reset_for_tests() -> None:
    _CACHE.reset()


def _install_for_tests(settings: Settings) -> None:
    """Make ``current()`` return exactly ``settings`` (no file check, no reload)
    until the next reset. The settings test fixture's one entry point."""
    with _CACHE._lock:
        _CACHE._settings = settings
        _CACHE._checked_at = float("inf")
        _CACHE._last_error = None


# Tests and the CLI may set the TTL; daemons use the default.
def set_ttl_for_tests(ttl_s: float) -> None:
    _CACHE.ttl_s = ttl_s
