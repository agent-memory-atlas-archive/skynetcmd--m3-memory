"""The config registry: every m3 setting, defined once.

A ``Key`` says where a setting lives in ``m3_config.toml`` (``toml_path``), how
an environment string becomes its value (``parse``), what is valid
(``validate``), what it defaults to, and which env names it answered to before
the registry existed (``env_names``, canonical first, then legacy aliases).

Everything keys on the REGISTRY, never on the ``M3_`` prefix. Nothing outside
this package interprets a setting's raw string: a legacy reader that parsed a
value differently keeps doing so through ``legacy_parse`` until 2D moves it,
and every such difference is on the intended-change list (§8).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

type ConfigValue = str | int | float | bool | Path | list[str]


class KeyClass(StrEnum):
    CONFIG = "config"  # user setting; lives in m3_config.toml
    SECRET = "secret"  # never in m3_config.toml; env > keyring > secrets file > vault
    STATE = "state"  # machine-written; never folded into TOML


class Scope(StrEnum):
    GLOBAL = "global"  # one value per machine
    PROCESS = "process"  # per process / call (the active store)
    LOCATOR = "locator"  # decides WHICH file is read


class ConfigValueError(ValueError):
    """A raw value that does not parse or validate for its key."""


# ── Canonical parsers (§2, #P11): ONE reading of a string per type ───────────
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


def parse_bool(raw: str) -> bool:
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigValueError(f"{raw!r} is not a boolean; use one of true/false, 1/0, yes/no, on/off")


def parse_int(raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigValueError(f"{raw!r} is not an integer") from None


def parse_float(raw: str) -> float:
    try:
        return float(raw.strip())
    except ValueError:
        raise ConfigValueError(f"{raw!r} is not a number") from None


def parse_str(raw: str) -> str:
    return raw


def parse_path(raw: str) -> Path:
    if not raw.strip():
        raise ConfigValueError("an empty string is not a path")
    return Path(raw.strip()).expanduser()


def parse_list(raw: str) -> list[str]:
    """Comma-separated in env; a TOML array in the file."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def one_of(*allowed: str) -> Callable[[str], None]:
    def check(value: str) -> None:
        if value not in allowed:
            raise ConfigValueError(f"{value!r} is not one of {', '.join(allowed)}")

    check.choices = allowed  # type: ignore[attr-defined]  # read by the schema generator
    return check


def choices_of(key: Key) -> tuple[str, ...] | None:
    """The allowed values of a ``one_of`` key, for generated docs and schema."""
    return getattr(key.validate, "choices", None)


def type_name(key: Key) -> str:
    """The TOML-native type of a key, for generated docs and schema."""
    return known_type(key) or "string"


def known_type(key: Key) -> str | None:
    """``type_name`` when the key uses a standard parser, else None (a key's own
    parser may produce anything: the static types must not guess "string")."""
    return _TYPE_NAMES.get(key.parse)


def in_range(lo: float | None = None, hi: float | None = None) -> Callable[[float], None]:
    def check(value: float) -> None:
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ConfigValueError(
                f"{value} is outside [{lo if lo is not None else '-inf'}, {hi if hi is not None else 'inf'}]"
            )

    return check


# The TOML-native type each parser produces, for validating file values.
_TOML_TYPE: dict[Callable[[str], object], tuple[type, ...]] = {
    parse_bool: (bool,),
    parse_int: (int,),
    parse_float: (int, float),
    parse_str: (str,),
    parse_path: (str,),
    parse_list: (list,),
}


_TYPE_NAMES: dict[Callable[[str], object], str] = {
    parse_bool: "boolean",
    parse_int: "integer",
    parse_float: "number",
    parse_str: "string",
    parse_path: "path",
    parse_list: "list of strings",
}


# eq=False: a key IS its registry object, compared and hashed by identity. A
# field-derived hash failed for any key with a list default (forbidden_hosts),
# so such a key could not be a dict key in writer.set_values / settings.override.
@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Key[T: ConfigValue]:
    """One setting. ``name`` is its identity: the dotted TOML path for a config
    key (``db.backend``), the secret's own name for a secret (``M3_PRIMARY_PG_URL``)."""

    name: str
    parse: Callable[[str], T]
    default: T | None
    doc: str
    klass: KeyClass = KeyClass.CONFIG
    bootstrap: bool = False  # secret resolved without the vault (§10)
    scope: Scope = Scope.GLOBAL
    env_names: tuple[str, ...] = ()  # canonical first, then legacy aliases
    reloadable: bool = True
    validate: Callable[[T], None] | None = None
    legacy_parse: Callable[[str], T] | None = None  # today's reading, where it differs
    machine_derived: bool = False
    since: str = "N"
    deprecated: str | None = None

    def __post_init__(self) -> None:
        if self.klass is KeyClass.SECRET and self.default is not None:
            raise ValueError(f"{self.name}: a secret has no default")
        if self.bootstrap and self.klass is not KeyClass.SECRET:
            raise ValueError(f"{self.name}: only a secret can be bootstrap")

    @property
    def table(self) -> str:
        return self.name.rpartition(".")[0]

    @property
    def leaf(self) -> str:
        return self.name.rpartition(".")[2]

    def coerce_file_value(self, raw: object) -> T:
        """A value read from TOML, checked against the key's TOML-native type."""
        expected = _TOML_TYPE.get(self.parse)
        if expected is not None and (not isinstance(raw, expected) or (isinstance(raw, bool) and bool not in expected)):
            raise ConfigValueError(
                f"expected {' or '.join(t.__name__ for t in expected)}, found {type(raw).__name__} {raw!r}"
            )
        converted: object = raw
        if self.parse is parse_path and isinstance(raw, str):
            converted = parse_path(raw)
        elif self.parse is parse_list and isinstance(raw, list):
            if not all(isinstance(x, str) for x in raw):
                raise ConfigValueError(f"expected a list of strings, found {raw!r}")
            converted = list(raw)
        elif self.parse is parse_float and isinstance(raw, int | float):
            converted = float(raw)
        value: T = converted  # type: ignore[assignment]  # checked against the parser's type above
        self.check(value)
        return value

    def coerce_env_value(self, raw: str) -> T:
        value = self.parse(raw)
        self.check(value)
        return value

    def check(self, value: T) -> None:
        if self.validate is not None:
            self.validate(value)


class Registry:
    """The keys, indexed by name and by every env name they answer to."""

    def __init__(self, keys: list[Key]) -> None:
        self._by_name: dict[str, Key] = {}
        self._by_env: dict[str, Key] = {}
        for key in keys:
            if key.name in self._by_name:
                raise ValueError(f"duplicate registry key {key.name}")
            self._by_name[key.name] = key
            for env in key.env_names:
                if env in self._by_env:
                    raise ValueError(f"env name {env} claimed by {self._by_env[env].name} and {key.name}")
                self._by_env[env] = key

    def __iter__(self):
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def get(self, name: str) -> Key | None:
        return self._by_name.get(name)

    def int_default(self, name: str) -> int:
        """The built-in default of an integer key (a port, a size). Raises on an
        unknown key or a non-integer default rather than handing back None."""
        key = self._by_name.get(name)
        if key is None:
            raise KeyError(f"unknown config key {name!r}")
        if isinstance(key.default, bool) or not isinstance(key.default, int):
            raise TypeError(f"config key {name!r} has no integer default (observed {key.default!r})")
        return key.default

    def by_env(self, env_name: str) -> Key | None:
        return self._by_env.get(env_name)

    def env_names(self) -> frozenset[str]:
        return frozenset(self._by_env)

    def tables(self) -> dict[str, list[Key]]:
        out: dict[str, list[Key]] = {}
        for key in self:
            if key.klass is KeyClass.CONFIG:
                out.setdefault(key.table, []).append(key)
        return out


# No key's env value outranks m3_config.toml: the one former exception
# (db.backend) was removed.
