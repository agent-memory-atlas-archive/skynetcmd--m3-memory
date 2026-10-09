"""Find and read m3_config.toml (CONFIG_DESIGN §3, §9). Python's ``tomllib`` is
the only parser of this file (single parser authority, m3 ``ab098fac``).

A bad file is refused with EVERY problem at once (``ConfigFileError``, an
``ExceptionGroup``): each names ``file: [table].key`` and the problem, never a
secret's value; notes add a best-effort line number, a did-you-mean hint and the
repair. Line numbers are best-effort by design: on 3.12 ``tomllib`` reports a
decode position only inside its message text, and none for a well-formed but
unknown key, so the source text is scanned (code never reads ``e.lineno``, 3.14).
"""
from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import override

from m3_core.paths import CONFIG_ROOT_LEGACY, StoreRef, config_root_with_tier

from .registry import ConfigValue, ConfigValueError, KeyClass, Registry
from .stores import parse_stores

logger = logging.getLogger(__name__)

CONFIG_FILE_NAME = "m3_config.toml"
SCHEMA_VERSION = 1
CONFIG_ARG_TIER = "arg:--config"
_REPAIR = "inspect: `m3 config show --origin`; fix or remove the line, then retry"


@dataclass(frozen=True, slots=True, kw_only=True)
class Location:
    file: Path
    root: Path
    tier: str  # which locator tier answered (§9)


def locate(explicit: str | os.PathLike[str] | None = None) -> Location:
    """``--config <file>`` > M3_CONFIG_ROOT > M3_MEMORY_ROOT/config (legacy, warns)
    > ~/.m3/config. ``explicit`` is a FILE, made absolute here; the root is its
    parent. The env tiers come from ``paths.config_root_with_tier``, their one owner."""
    if explicit is not None:
        f = Path(explicit).expanduser().absolute()
        return Location(file=f, root=f.parent, tier=CONFIG_ARG_TIER)
    root, tier = config_root_with_tier()
    if tier == CONFIG_ROOT_LEGACY:
        _warn_legacy_once(root)
    r = Path(root)
    return Location(file=r / CONFIG_FILE_NAME, root=r, tier=tier)


_LEGACY_WARNED: set[str] = set()


def _warn_legacy_once(root: str) -> None:
    if root not in _LEGACY_WARNED:
        _LEGACY_WARNED.add(root)
        logger.warning(
            "The m3 config root comes from the legacy M3_MEMORY_ROOT/config tier (%s), which is "
            "removed at N+3. Set M3_CONFIG_ROOT, or pass --config <file>.", root)


# ── Errors ───────────────────────────────────────────────────────────────────
class ConfigKeyError(ValueError):
    """One invalid entry of one config file."""

    def __init__(self, file: Path, key: str, problem: str) -> None:
        super().__init__(f"{file}: {show_key(key)}: {problem}")
        self.file = file
        self.key = key


class ConfigSyntaxError(ValueError):
    """The file is not valid TOML (tomllib's message carries the position)."""


class ConfigFileError(ExceptionGroup):
    """Every problem found in one config file, reported at once (§3)."""

    @override
    def derive(self, excs):  # keep the type through split()/subgroup()
        return ConfigFileError(self.message, excs)


def format_config_error(exc: BaseException) -> str:
    """The ONE rendering of a config error for logs and the CLI: every
    sub-error with its notes. ``str()`` of a group shows only the header, and a
    note never appears in ``str()``, so a log line built any other way loses the
    key, the line and the repair (DESIGN_PHILOSOPHIES §3)."""
    def one(e: BaseException, indent: str) -> list[str]:
        lines = [f"{indent}{e}"]
        pad = " " * (len(indent) + 2)  # notes sit under their error, not as new bullets
        lines += [f"{pad}{n}" for n in getattr(e, "__notes__", ())]
        return lines

    if isinstance(exc, BaseExceptionGroup):
        out = [str(exc.message)]
        for sub in exc.exceptions:
            out += one(sub, "  - ")
        return "\n".join(out)
    return "\n".join(one(exc, ""))


def show_key(key: str) -> str:
    table, _, leaf = key.rpartition(".")
    return f"[{table}].{leaf}" if table else leaf


# ── Reading ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True, kw_only=True)
class FileIdentity:
    """What the reload check compares (§3): not (mtime_ns, size), which misses
    ~60% of atomic replaces on NTFS; st_ino catches those, and the content hash
    covers ext4, where the inode can alternate between two values."""

    dev: int
    ino: int
    mtime_ns: int
    size: int
    sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class LoadedFile:
    location: Location
    identity: FileIdentity | None          # None: the file does not exist
    values: Mapping[str, ConfigValue] = field(default_factory=lambda: MappingProxyType({}))
    schema_version: int = SCHEMA_VERSION
    warnings: tuple[str, ...] = ()
    stores: Mapping[str, StoreRef] = field(default_factory=lambda: MappingProxyType({}))


def _read(path: Path) -> tuple[bytes, FileIdentity] | None:
    """The file's bytes and the identity OF THOSE BYTES (hashed from what was
    read, so a reload check never compares against content it did not parse)."""
    try:
        data = path.read_bytes()
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return data, FileIdentity(dev=st.st_dev, ino=st.st_ino, mtime_ns=st.st_mtime_ns,
                              size=st.st_size, sha256=hashlib.sha256(data).hexdigest())


def file_identity(path: Path) -> FileIdentity | None:
    got = _read(path)
    return got[1] if got else None


def load(location: Location, registry: Registry) -> LoadedFile:
    """Read and validate the file at ``location``. An absent file is not an
    error (every key takes its default); a present, invalid one raises
    ``ConfigFileError`` naming every problem."""
    path = location.file
    got = _read(path)
    if got is None:
        return LoadedFile(location=location, identity=None)
    data, identity = got
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _syntax_error(path, e) from None
    values, schema, warnings, stores = validate_text(path, text, registry)
    return LoadedFile(location=location, identity=identity, values=MappingProxyType(values),
                      schema_version=schema, warnings=tuple(warnings), stores=MappingProxyType(stores))


def _syntax_error(path: Path, e: Exception) -> ConfigFileError:
    err = ConfigSyntaxError(f"{path}: not valid TOML: {e}")
    err.add_note("fix the file at the position named, or move it aside to start from defaults")
    return ConfigFileError(f"{path} could not be read", [err])


def validate_text(path: Path, text: str, registry: Registry
                  ) -> tuple[dict[str, ConfigValue], int, list[str], dict[str, StoreRef]]:
    """THE validation of m3_config.toml content: (values, schema_version, warnings, stores),
    or ``ConfigFileError`` naming every problem. The loader runs it on the file;
    the writer runs it on the NEW text before replacing the file (§12)."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise _syntax_error(path, e) from None

    errors: list[Exception] = []
    warnings: list[str] = []
    schema = doc.pop("schema_version", SCHEMA_VERSION)
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        errors.append(_key_error(path, text, "schema_version",
                                 f"must be a positive integer, found {schema!r}"))
        schema = SCHEMA_VERSION
    newer = schema > SCHEMA_VERSION

    # [stores.<name>] tables are dynamic: validated by the stores module (§14).
    stores = parse_stores(doc.pop("stores"), lambda key, msg: errors.append(_key_error(path, text, key, msg)))         if "stores" in doc else {}

    values: dict[str, ConfigValue] = {}
    for name, raw in _leaves(doc):
        key = registry.get(name) or registry.by_env(name.rpartition(".")[2])
        if key is not None and key.klass is KeyClass.SECRET:
            # Never echo the value: it is the hazard (§10 Secret Invariant).
            errors.append(_key_error(
                path, text, name, f"{key.name} is a secret and is never read from this file",
                "move it to the keyring or the secrets file, then delete this line"))
            continue
        if key is not None and key.name != name:
            errors.append(_key_error(path, text, name,
                                     f"{name.rpartition('.')[2]} is an environment variable name",
                                     f"in this file the setting is {show_key(key.name)}"))
            continue
        if key is None:
            problem = "unknown key"
            if newer:
                warnings.append(f"{path}: {show_key(name)}: unknown to schema {SCHEMA_VERSION}, "
                                f"file declares schema {schema}; ignored")
                continue
            errors.append(_key_error(path, text, name, problem,
                                     _did_you_mean(name, registry)))
            continue
        if key.klass is KeyClass.STATE:
            errors.append(_key_error(path, text, name, "is machine state and is not configurable"))
            continue
        try:
            values[name] = key.coerce_file_value(raw)
        except ConfigValueError as e:
            errors.append(_key_error(path, text, name, str(e)))

    if errors:
        raise ConfigFileError(f"{path} has {len(errors)} invalid "
                              f"{'entry' if len(errors) == 1 else 'entries'}", errors)
    return values, schema, warnings, stores


def _leaves(doc: Mapping, prefix: str = ""):
    """Dotted names of the non-table values. An array of tables is a leaf
    (its key then fails as unknown or mistyped, loudly)."""
    for k, v in doc.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _leaves(v, f"{name}.")
        else:
            yield name, v


def _key_error(path: Path, text: str, name: str, problem: str, hint: str | None = None) -> ConfigKeyError:
    err = ConfigKeyError(path, name, problem)
    line = _find_line(text, name)
    if line is not None:
        err.add_note(f"line {line} (best effort)")
    if hint:
        err.add_note(hint)
    err.add_note(_REPAIR)
    return err


def _did_you_mean(name: str, registry: Registry) -> str | None:
    names = [k.name for k in registry if k.klass is KeyClass.CONFIG]
    near = difflib.get_close_matches(name, names, n=1, cutoff=0.6)
    return f"did you mean {show_key(near[0])}?" if near else None


_HEADER = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")


def _find_line(text: str, name: str) -> int | None:
    """Best-effort 1-based line of ``name``: ``leaf =`` under ``[table]``, or a
    dotted ``table.leaf =`` at the top level. None when not found."""
    table, _, leaf = name.rpartition(".")
    current = ""
    for n, line in enumerate(text.splitlines(), start=1):
        m = _HEADER.match(line)
        if m:
            current = m.group(1).replace(" ", "")
            continue
        assigned = line.split("=", 1)[0].strip().replace(" ", "") if "=" in line else None
        if assigned is None or line.lstrip().startswith("#"):
            continue
        full = f"{current}.{assigned}" if current else assigned
        if full == name or (current == table and assigned == leaf):
            return n
    return None
