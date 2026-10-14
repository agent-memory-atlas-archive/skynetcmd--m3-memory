"""The m3 root directories: the ONE owner of where config, engine, logs and models live.

Precedence for each root ``X`` in (config, engine, logs, models)::

    M3_X_ROOT  >  M3_MEMORY_ROOT/<x>  >  ~/.m3/<x>

and the memory root itself is ``M3_MEMORY_ROOT`` or ``~/.m3-memory``. A blank
value counts as unset (R-blank).

WHY THIS MODULE IS PURE. It imports the standard library only. The root rule was
copied into ~20 files because their code must run where m3_core is not
importable: the installed package (``m3_memory/``), the crypto layer, the chatlog
hooks that run under whatever Python an agent has, and fallbacks for a broken
``m3_sdk``. Each copy carried a comment defending it, and the copies drifted
(the models root alone ignored POSIX-style roots on Windows). A module that pulls
in nothing can be imported from every one of those places, so the copies are
deleted rather than exempted. ``m3_core.paths``
delegates here; ``bin/`` reaches it through ``m3_core.seams``.

Roots are OS-varying defaults (home directory, separators), so this sits beside
the OS seam, below the database layer, and may import only the stdlib and
``m3_memory._pyver`` / ``m3_memory._platform`` (pinned by tests/test_seam_integrity.py).
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import tomllib

MEMORY_ROOT_ENV = "M3_MEMORY_ROOT"
CONFIG_ROOT_ENV_NAME = "M3_CONFIG_ROOT"
ENGINE_ROOT_ENV = "M3_ENGINE_ROOT"
LOGS_ROOT_ENV = "M3_LOGS_ROOT"
MODELS_ROOT_ENV = "M3_MODELS_ROOT"

# Which tier of the config-root precedence answered. The
# loader reports it (`m3 config show --origin`) and warns on the legacy tier.
CONFIG_ROOT_ENV = "env:M3_CONFIG_ROOT"
CONFIG_ROOT_LEGACY = "legacy:M3_MEMORY_ROOT/config"
CONFIG_ROOT_DEFAULT = "default:~/.m3/config"
CONFIG_ROOT_ARG = "arg:--config"
CONFIG_FILE_NAME = "m3_config.toml"


def abs_root(root: str) -> str:
    """Expand `~` and make relative roots absolute, WITHOUT rewriting a path that
    is already absolute under either path convention.

    `os.path.abspath` only understands the host convention. On Windows a POSIX
    root such as `/data/.m3` (what a user gets by exporting a root from Git Bash
    or WSL) looks relative, so abspath silently prefixes the current drive and
    yields `C:\\data\\.m3`. The server then points at a location the user never
    chose, and the real store is orphaned. Absolute roots are returned
    normalised (forward slashes) and otherwise untouched.
    """
    p = os.path.expanduser(root)
    if posixpath.isabs(p) or ntpath.isabs(p):
        return p.replace("\\", "/")
    return os.path.abspath(p).replace("\\", "/")


def _env(name: str) -> str | None:
    """The variable's value, or None when unset or blank (R-blank)."""
    return os.environ.get(name) or None  # env-owner: the approved root locators


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def pinned(env_name: str) -> str | None:
    """The root a variable pins EXPLICITLY (absolute), or None when unset or blank.

    For callers whose behaviour depends on whether the user chose a root, not
    just on where it resolves (the installer's "is this root pinned?", a search
    path that applies only when M3_MEMORY_ROOT is set).
    """
    root = _env(env_name)  # env-owner: the approved root locators
    return abs_root(root) if root else None


MIGRATED_MARKER = ".memory_root_migrated"


def memory_root_pin() -> str | None:
    """The memory root ``M3_MEMORY_ROOT`` pins explicitly, or None. The one read of
    that name for Root-1: the legacy root, the default and the migration all ask here."""
    return pinned(MEMORY_ROOT_ENV)  # env-owner: the approved root locators


def legacy_memory_root() -> str:
    """Where pre-decoupling state lives: ``M3_MEMORY_ROOT`` when pinned, else
    ``~/.m3-memory``. Legacy lookups (the old salt location, the .env fallback, the
    old ``memory/`` databases) read it through the deprecation window, whatever
    ``memory_root()`` resolves to. They must NOT ask ``memory_root()``: after the
    Root-1 flip that answers ``~/.m3``, and a pre-Homecoming install whose
    databases still sit in ``~/.m3-memory/memory/`` would stop finding them."""
    return memory_root_pin() or _home(".m3-memory")


def memory_root_migrated() -> bool:
    """True once ``homecoming.migrate_memory_root`` has moved the legacy root's
    state into ``~/.m3`` (it writes the marker last)."""
    return os.path.isfile(_home(".m3", MIGRATED_MARKER))


def memory_root() -> str:
    """The memory root: ``M3_MEMORY_ROOT``; else ``~/.m3`` once the legacy state has
    been migrated there; else the legacy ``~/.m3-memory``.

    Root-1: the default moves to
    ``~/.m3`` only AFTER the migration ran, so an un-migrated install keeps its old
    root instead of splitting its audit trail and config across two directories."""
    if memory_root_pin() or not memory_root_migrated():
        return legacy_memory_root()  # the pin, or the un-migrated ~/.m3-memory
    return _home(".m3")


def _derived(env_name: str, sub: str) -> str:
    """``M3_X_ROOT`` > ``M3_MEMORY_ROOT/<sub>`` > ``~/.m3/<sub>``."""
    root = _env(env_name)  # env-owner: the approved root locators
    if root:
        return abs_root(root)
    mem = _env(MEMORY_ROOT_ENV)  # env-owner: the approved root locators
    if mem:
        return posixpath.join(abs_root(mem), sub)
    return _home(".m3", sub)


_pinned_config: str | None = None


def pin_config_file(path: str | os.PathLike[str] | None) -> None:
    """``m3 --config FILE``: this process reads FILE and its directory is the
    config root, above ``M3_CONFIG_ROOT``. Process-wide, like
    ``--store``'s process layer; a child gets ``--config`` (``_childenv``), never
    env. ``None`` clears it. The file need not exist yet: setup creates it."""
    global _pinned_config
    _pinned_config = abs_root(os.fspath(path)) if path is not None else None


def pinned_config_file() -> str | None:
    return _pinned_config


def pin_from_argv(argv: list[str] | None = None) -> None:
    """Pin ``--config FILE`` / ``--config=FILE`` from a script's argv BEFORE its
    m3 imports run. Several modules bind roots at import (chatlog_config's paths,
    log dirs), so the argparse pin (``add_config_arg``) comes too late for them;
    an inherited env var never did. Call it as a scheduled entry script's first
    statement. A ``--config`` with no value is left for argparse to reject."""
    if argv is None:
        import sys

        argv = sys.argv
    for i, arg in enumerate(argv[1:], start=1):
        if arg.startswith("--config="):
            pin_config_file(arg.split("=", 1)[1])
            return
        if arg == "--config" and i + 1 < len(argv):
            pin_config_file(argv[i + 1])
            return


def located_config_args() -> list[str]:
    """``--config FILE`` for a job that runs WITHOUT this process's environment (a
    scheduled task, a service): the config file this process located, unless that
    is the default one, which the job finds by itself. Covers a ``--config`` pin
    AND ``M3_CONFIG_ROOT`` / the legacy tier: a job registered from a shell that
    exports M3_CONFIG_ROOT would otherwise run against the default store."""
    if config_root_with_tier()[1] == CONFIG_ROOT_DEFAULT:
        return []
    return ["--config", config_file()]


def config_args() -> list[str]:
    """``--config FILE`` for an m3 CLI child when this process has a pin; [] otherwise
    (the child locates the same file the usual way). The pin reaches children as an
    argument, never as env."""
    return ["--config", _pinned_config] if _pinned_config is not None else []


def config_root_with_tier() -> tuple[str, str]:
    """(config root, tier): which precedence tier answered, for `m3 config show`."""
    if _pinned_config is not None:
        return posixpath.dirname(_pinned_config), CONFIG_ROOT_ARG
    if _env(CONFIG_ROOT_ENV_NAME):  # env-owner: the approved root locators
        # env-owner: the approved root locators
        return _derived(CONFIG_ROOT_ENV_NAME, "config"), CONFIG_ROOT_ENV
    if _env(MEMORY_ROOT_ENV):  # env-owner: the approved root locators
        # env-owner: the approved root locators
        return _derived(CONFIG_ROOT_ENV_NAME, "config"), CONFIG_ROOT_LEGACY
    # env-owner: the approved root locators
    return _derived(CONFIG_ROOT_ENV_NAME, "config"), CONFIG_ROOT_DEFAULT


def config_root() -> str:
    if _pinned_config is not None:
        return posixpath.dirname(_pinned_config)
    return _derived(CONFIG_ROOT_ENV_NAME, "config")  # env-owner: the approved root locators


def config_file() -> str:
    """The m3_config.toml this process reads: the ``--config`` file, else
    ``<config root>/m3_config.toml``."""
    return _pinned_config or posixpath.join(config_root(), CONFIG_FILE_NAME)


_file_key: tuple[str, int, int] | None = None
_file_values: dict[str, str] = {}


def _file_paths() -> dict[str, str]:
    """``[paths]`` from ``<config root>/m3_config.toml``, as strings; {} without a file.

    Roots resolve FILE first, then the env var read LIVE, then the derived default. Read here with stdlib ``tomllib``, NOT
    through the registry: the registry sits above the database seam and this module
    beside the OS seam, so asking it would be an upward import (§10a), and its
    ``Path`` parsing breaks under a simulated OS. Strings through ``abs_root`` are
    convention-aware. Cached on the file's identity; a malformed file raises, as
    the loader does (§3). tests/test_roots_from_file.py pins parity with
    ``settings.setting("paths.*")``."""
    global _file_key, _file_values
    path = config_file()
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (path, st.st_mtime_ns, st.st_size)
    if _file_key != key:
        with open(path, "rb") as fh:
            table = tomllib.load(fh).get("paths") or {}
        paths: dict[str, str] = {}
        for name in ("engine_root", "logs_root", "models_root"):
            if name not in table:
                continue
            value = table[name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"[paths] {name} in {path} must be a non-empty path string, found {value!r}")
            paths[name] = value.strip()
        _file_key, _file_values = key, paths
    return _file_values


def _file_root(name: str) -> str | None:
    """``[paths] <name>`` from m3_config.toml, made absolute; None when unset.
    The file outranks the env var and the derived default."""
    value = _file_paths().get(name)
    return abs_root(value) if value else None


def engine_root() -> str:
    # env-owner: the approved root locators
    return _file_root("engine_root") or _derived(ENGINE_ROOT_ENV, "engine")


def logs_root() -> str:
    """Logs are user state, not payload: never under the install root, where a
    reinstall deletes them."""
    # env-owner: the approved root locators
    return _file_root("logs_root") or _derived(LOGS_ROOT_ENV, "logs")


LIB_DIR_ENV = "M3_LIB_DIR"


def lib_root() -> str:
    """m3's own trusted native-library directory (wolfSSL): ``~/.m3/lib`` by default.

    Precedence: ``M3_LIB_DIR`` > the config root's PARENT ``/lib`` >
    ``M3_MEMORY_ROOT/lib`` > ``~/.m3/lib``. Unlike the other roots it hangs off
    the config root's parent, and it keeps host-convention ``abspath``: the crypto
    loader and the wolfSSL installer each carried a hand-synced copy of exactly
    this rule, and this is that rule, unchanged. Relocating it matters: anything
    that repoints the config root would otherwise move the trusted search away
    from the installed library, which is fail-closed under M3_FIPS_MODE=1.
    """
    # env-owner: the approved root locators
    pinned = (os.environ.get(LIB_DIR_ENV) or "").strip()
    if pinned:
        return os.path.abspath(os.path.expanduser(pinned))
    cfg = _env(CONFIG_ROOT_ENV_NAME)  # env-owner: the approved root locators
    if cfg:
        base = os.path.dirname(os.path.abspath(os.path.expanduser(cfg)))
    else:
        mem = _env(MEMORY_ROOT_ENV)  # env-owner: the approved root locators
        base = os.path.abspath(os.path.expanduser(mem)) if mem else _home(".m3")
    return os.path.join(base, "lib")


def models_root() -> str:
    """GGUF weights m3 owns itself. Probed FIRST among model locations: every
    other one belongs to a tool the user may uninstall."""
    # env-owner: the approved root locators
    return _file_root("models_root") or _derived(MODELS_ROOT_ENV, "models")
