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

from m3_core.paths import StoreRef, pinned_config_file

from . import env as _env
from .keys import REGISTRY
from .loader import FileIdentity, LoadedFile, Location, file_identity, format_config_error, load, locate
from .registry import ConfigValue, Key, KeyClass, Registry
from .resolve import ConfigEnvError, DotEnv, Resolved, from_env, resolve_all

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

    def llm_file_in_use(self) -> list[str]:
        """Names whose value in use comes from the legacy .llm_config.json: what
        import-env moves into [llm] (fold plan 2026.10.12, step 3)."""
        return sorted(r.env_name for r in self.resolved.values() if r.origin.startswith("llmfile:") and r.env_name)

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
    llmfile: DotEnv | None = None,
) -> Settings:
    """Load and resolve a snapshot. Raises ``ConfigFileError`` / ``ConfigEnvErrors``.
    A caller passing its own ``environ`` gets no legacy file it did not pass too."""
    from .keys import RETIRED

    loc = location if location is not None else locate()
    loaded: LoadedFile = load(loc, registry)
    env = environ if environ is not None else _env.environ()
    if dotenv is None:
        path, values = _env.dotenv() if environ is None else (None, {})
        dotenv = DotEnv(path=path, values=values)
    if llmfile is None:
        path, values = _env.llm_file() if environ is None else (None, {})
        llmfile = DotEnv(path=path, values=values, kind="llmfile")
    resolved = resolve_all(registry, loaded, env, dotenv, llmfile)
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
        self._pin: str | None = None  # the `m3 --config` file the snapshot was built for
        self.ttl_s = DEFAULT_TTL_S

    def _with_cli(self, settings: Settings) -> Settings:
        if not self._cli:
            return settings
        resolved = dict(settings.resolved)
        for key, value in self._cli.items():
            resolved[key.name] = Resolved(value=value, origin="cli")
        return dataclasses.replace(settings, resolved=MappingProxyType(resolved))

    def cli_values(self) -> dict[Key, ConfigValue]:
        with self._lock:
            return dict(self._cli)

    def set_cli(self, values: Mapping[Key, ConfigValue]) -> None:
        with self._lock:
            self._cli.update(values)
            if self._settings is not None:
                self._settings = self._with_cli(self._settings)

    def current(self) -> Settings:
        with self._lock:
            now = time.monotonic()
            pin = pinned_config_file()
            if pin != self._pin:  # `--config` chosen (or changed) after a snapshot: another file
                self._settings, self._pin = None, pin
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


SET_FLAG = "--set"
_FORMAT = f'm3 "{SET_FLAG}={{key=value, ...}}" <command>'
_EXAMPLE = f'"{SET_FLAG}={{db.backend=^postgres^, search.row_cap=50}}"'
# The string quote inside --set (user decision, m3 7612b947 refined): survives
# double quotes in bash, zsh, PowerShell and cmd.exe alike (measured
# 2026-10-12), so one quoting rule works in every shell. ^^ is a literal ^.
_CARET = "^"


def _carets_to_toml(text: str) -> str:
    """``^...^`` strings become TOML strings; ``^^`` inside one is a literal ``^``.
    A ``^`` inside a TOML ``"..."`` or ``'...'`` string is left as it is, so the
    file's own syntax still works. Raises OverrideError for an unclosed string
    (naming its position, never its text: it could hold a secret)."""
    import json

    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == _CARET:
            start, i, buf = i, i + 1, []
            while True:
                if i >= n:
                    raise OverrideError(
                        [
                            f"the {_CARET} string at character {start + 1} is not closed; end it with {_CARET} ({_CARET * 2} is a literal {_CARET})"
                        ]
                    )
                if text[i] == _CARET:
                    if i + 1 < n and text[i + 1] == _CARET:
                        buf.append(_CARET)
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(text[i])
                i += 1
            out.append(json.dumps("".join(buf)))  # a JSON string is a valid TOML basic string
        elif c in ('"', "'"):
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if (c == '"' and text[j] == "\\") else 1
            out.append(text[i : j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


class OverrideError(ValueError):
    """A refused ``--set``: every problem, the scope (nothing applied), and what
    to do. Never echoes a value that could be a secret."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "\n".join(
                [
                    f"observed: {SET_FLAG} refused; nothing was applied and the command did not run:",
                    *(f"  - {p}" for p in problems),
                    f"inspect: the format is {_FORMAT} (one table in double quotes, right after m3; strings in ^...^); "
                    "`m3 config show` lists every setting and its value",
                ]
            )
        )


def flag_overridable(key: Key) -> bool:
    """Whether ``--set`` may change ``key``: a CONFIG key that does not decide
    where m3 keeps its data (a LOCATOR: the roots, the secrets file; use
    ``--config``). Secrets are never flags: a command line is visible to other
    users and kept in shell history (user decision, m3 a6cdfc48)."""
    from .registry import Scope

    return key.klass is KeyClass.CONFIG and key.scope is not Scope.LOCATOR


def _flatten(table: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for name, value in table.items():
        dotted = f"{prefix}{name}"
        if isinstance(value, dict) and REGISTRY.get(dotted) is None:
            out.update(_flatten(value, dotted + "."))
        else:
            out[dotted] = value
    return out


def _unknown(name: str) -> str:
    """Why ``name`` is not a setting, with the nearest real names."""
    import difflib

    env_key = REGISTRY.by_env(name)
    if env_key is not None:
        return f"{name} is an environment-variable name; the setting is {env_key.name}"
    names = sorted(k.name for k in REGISTRY if k.klass is KeyClass.CONFIG)
    near = difflib.get_close_matches(name, names, n=2)
    return f"{name} is not a setting" + (f"; did you mean {' or '.join(near)}?" if near else "")


def _secret(name: str) -> str:
    try:
        from m3_core.secret_store import secrets_file_path

        where = f"the secrets file {secrets_file_path()}"
    except Exception:  # noqa: BLE001 -- the hint degrades, the refusal stands
        where = "the secrets file"
    return f"{name} is a secret, and secrets are never flags (a command line is visible to others): put it in {where}"


def _locator(key: Key) -> str:
    table, _, leaf = key.name.rpartition(".")
    return (
        f"{key.name} decides which files m3 reads or writes, so it is not a flag: set it in a config file "
        f"([{table}] {leaf} = ...) and run m3 --config FILE <command>"
    )


def parse_set(text: str) -> dict[str, ConfigValue]:
    """The ``{...}`` of ``--set={...}``: a TOML inline table, the same key names
    and syntax as m3_config.toml (user decision, m3 7612b947). Each value is
    checked by its key exactly as the file's would be. Raises OverrideError
    naming EVERY problem; nothing is applied unless all are valid."""
    import tomllib

    from .registry import ConfigValueError

    text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        # Never echo the token: it could carry a secret (--set=M3_X=<password>).
        shown = text.split("=", 1)[0][:40] + ("=..." if "=" in text else "")
        raise OverrideError(
            [
                f"got {SET_FLAG}={shown}, not a {{...}} table. If you typed braces, your shell split "
                f"them (bash and zsh expand {{a,b}} into separate words): quote the whole token, e.g. {_EXAMPLE}"
            ]
        )
    try:
        table = tomllib.loads("set = " + _carets_to_toml(text))["set"]
    except tomllib.TOMLDecodeError as e:
        import re

        # tomllib names a column in the TRANSLATED text, which would point at the
        # wrong character of what was typed: keep the reason, drop the position.
        # Never the text itself: it could hold a secret.
        reason = re.sub(r"\s*\(at line \d+, column \d+\)", "", str(e))
        raise OverrideError(
            [
                f"the table does not parse ({reason}). Strings use {_CARET}...{_CARET} "
                f"(db.backend={_CARET}postgres{_CARET}), lists [...] (llm.endpoints=[{_CARET}http://a/v1{_CARET}]), "
                "numbers and true/false bare"
            ]
        ) from None
    out: dict[str, ConfigValue] = {}
    problems: list[str] = []
    for name, raw in _flatten(table).items():
        key = REGISTRY.get(name)
        if key is None:
            env_key = REGISTRY.by_env(name)
            problems.append(
                _secret(name) if env_key is not None and env_key.klass is KeyClass.SECRET else _unknown(name)
            )
        elif key.klass is KeyClass.SECRET:
            problems.append(_secret(name))
        elif not flag_overridable(key):
            problems.append(_locator(key))
        else:
            try:
                value = key.coerce_file_value(raw)
                key.check(value)
                out[name] = value
            except ConfigValueError as e:
                # A setting's value, never a secret's (refused above): safe to show.
                problems.append(f"{name} = {raw!r} is not valid: {e}")
    if problems:
        raise OverrideError(problems)
    return out


def _is_set(arg: str) -> bool:
    return arg == SET_FLAG or arg.startswith(SET_FLAG + "=")


def split_set(argv: list[str]) -> tuple[str | None, list[str]]:
    """``(the {...} text or None, argv without it)``. The token's place is fixed:
    the first argument after the program. A ``--set`` anywhere else (before a
    bare ``--``) is refused, never passed downstream."""
    rest = list(argv)
    text: str | None = None
    if len(rest) > 1 and _is_set(rest[1]):
        token = rest.pop(1)
        if token == SET_FLAG:
            if len(rest) < 2 or rest[1].startswith("-"):
                raise OverrideError([f"{SET_FLAG} has no table after it, e.g. {_EXAMPLE}"])
            text = rest.pop(1)
        else:
            text = token[len(SET_FLAG) + 1 :]
    for i, arg in enumerate(rest[1:], start=1):
        if arg == "--":
            break
        if not _is_set(arg):
            continue
        if text is not None and i == 1:
            if not text.strip().startswith("{"):
                raise OverrideError(
                    [
                        f"{SET_FLAG} came in pieces: your shell split the braces into separate words "
                        f"(bash and zsh expand {{a,b}}). Quote the whole token, e.g. {_EXAMPLE}"
                    ]
                )
            raise OverrideError([f"only one {SET_FLAG} is allowed: put every setting in one table, e.g. {_EXAMPLE}"])
        raise OverrideError(
            [
                f"{SET_FLAG} is argument {i}, after {rest[1]!r}: it goes immediately after the program name, "
                f"before the command, e.g. m3 {_EXAMPLE} search ..."
            ]
        )
    return text, rest


def apply_set(argv: list[str]) -> list[str]:
    """THE pre-parser: read ``--set`` once, apply it to this process (origin
    ``cli``; every function, thread and task reads it through ``setting()``), and
    return argv without it, for the command to parse untouched."""
    text, rest = split_set(argv)
    if text is not None:
        set_from_cli(parse_set(text))
    return rest


def cli_args() -> list[str]:
    """``[--set={...}]`` with every flag-set value of this process, for an m3
    child's FIRST argument: a new process starts with an empty snapshot, so the
    parent hands its values over at the spawn point (beside ``config_args()``)."""
    from .toml_render import toml_value

    values = {k: v for k, v in _CACHE.cli_values().items() if flag_overridable(k)}
    if not values:
        return []
    body = ", ".join(f"{k.name} = {toml_value(v)}" for k, v in sorted(values.items(), key=lambda kv: kv[0].name))
    return [f"{SET_FLAG}={{{body}}}"]


def with_flags(settings: Settings) -> Settings:
    """``settings`` with this process's flag values on top (origin ``cli``), as
    ``current()`` serves them: a snapshot built for display (`m3 config show`,
    `get`) must not report a ``--set`` value as the file's or the default."""
    with _CACHE._lock:
        return _CACHE._with_cli(settings)


def origin_of(name: str) -> str:
    """Where key ``name``'s value came from: ``default``, ``env:<NAME>``,
    ``env-legacy:<NAME>`` or the config file. The reporting path for diagnostics
    that ask "is this pinned in the process environment?" (CONFIG_DESIGN §2,
    ignored-env reporting): they read the origin, not os.environ."""
    key = REGISTRY.get(name)
    if key is None:
        raise KeyError(f"observed: {name!r} is not a registry key. inspect: m3_core/config/keys*.py")
    return current().origin(key)


def file_value(name: str) -> ConfigValue | None:
    """Key ``name``'s value when m3_config.toml, a command-line flag or a test
    override set it; None when it would come from the environment, a legacy file
    or the default. For readers that keep a legacy JSON file BETWEEN m3_config.toml
    and the environment (the autonomy toggles, the governor thresholds; <dynamic>
    design Q2, 2026-10-13): file_value, else the JSON file, else setting()."""
    key = REGISTRY.get(name)
    if key is None:
        raise KeyError(f"observed: {name!r} is not a registry key. inspect: m3_core/config/keys*.py")
    snap = current()
    origin = snap.origin(key)
    return snap[key] if origin.startswith("file:") or origin in ("cli", "override") else None


@dataclass(frozen=True, slots=True, kw_only=True)
class EnvDisagreement:
    """An environment variable whose value contradicts m3_config.toml."""

    key: str
    env_name: str
    env_value: ConfigValue | str  # the raw text when it does not even parse
    file_value: ConfigValue | None  # what m3 uses: the file always wins


def env_contradicting(key: Key, value: ConfigValue | None) -> list[tuple[str, ConfigValue | str]]:
    """Each of ``key``'s env names set in the LIVE environment to something other
    than ``value``, with what it reads as. A matching value is not listed: the
    transition report must not cry wolf (user decision 2026-10-12, m3 64ea4eab)."""
    environ = _env.environ()
    out: list[tuple[str, ConfigValue | str]] = []
    for name in key.env_names:
        raw = environ.get(name)
        if raw is None or not raw.strip():
            continue
        try:
            seen: ConfigValue | str = from_env(key, name, raw)
        except ConfigEnvError:
            seen = raw
        if seen != value:
            out.append((name, seen))
    return out


def env_disagreements() -> list[EnvDisagreement]:
    """Every env var that contradicts a value SET in m3_config.toml (the file
    wins). The transition report ``m3 doctor`` and setup print until the user
    retires it (user decision 2026-10-12, m3 64ea4eab). A key with no value in
    the file, or one a flag set for this run, has nothing to disagree with."""
    snap = current()
    out: list[EnvDisagreement] = []
    for key in REGISTRY:
        if key.klass is not KeyClass.CONFIG or key.name not in snap.resolved:
            continue
        r = snap.resolved[key.name]
        if not r.origin.startswith("file:"):
            continue
        out.extend(
            EnvDisagreement(key=key.name, env_name=n, env_value=v, file_value=r.value)
            for n, v in env_contradicting(key, r.value)
        )
    return out


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
