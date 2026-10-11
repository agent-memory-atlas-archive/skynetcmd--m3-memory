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
deleted rather than exempted (user decision 2026-10-11). ``m3_core.paths``
delegates here; ``bin/`` reaches it through ``m3_core.seams``.

Roots are OS-varying defaults (home directory, separators), so this sits beside
the OS seam, below the database layer, and may import only the stdlib and
``m3_memory._pyver`` / ``m3_memory._platform`` (pinned by tests/test_seam_integrity.py).
"""

from __future__ import annotations

import ntpath
import os
import posixpath

MEMORY_ROOT_ENV = "M3_MEMORY_ROOT"
CONFIG_ROOT_ENV_NAME = "M3_CONFIG_ROOT"
ENGINE_ROOT_ENV = "M3_ENGINE_ROOT"
LOGS_ROOT_ENV = "M3_LOGS_ROOT"
MODELS_ROOT_ENV = "M3_MODELS_ROOT"

# Which tier of the config-root precedence answered (CONFIG_DESIGN §9). The
# loader reports it (`m3 config show --origin`) and warns on the legacy tier.
CONFIG_ROOT_ENV = "env:M3_CONFIG_ROOT"
CONFIG_ROOT_LEGACY = "legacy:M3_MEMORY_ROOT/config"
CONFIG_ROOT_DEFAULT = "default:~/.m3/config"


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
    return os.environ.get(name) or None


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def memory_root() -> str:
    """The memory root: ``M3_MEMORY_ROOT``, else ``~/.m3-memory``."""
    root = _env(MEMORY_ROOT_ENV)
    if root:
        return abs_root(root)
    return _home(".m3-memory")


def _derived(env_name: str, sub: str) -> str:
    """``M3_X_ROOT`` > ``M3_MEMORY_ROOT/<sub>`` > ``~/.m3/<sub>``."""
    root = _env(env_name)
    if root:
        return abs_root(root)
    mem = _env(MEMORY_ROOT_ENV)
    if mem:
        return posixpath.join(abs_root(mem), sub)
    return _home(".m3", sub)


def config_root_with_tier() -> tuple[str, str]:
    """(config root, tier): which precedence tier answered, for `m3 config show`."""
    if _env(CONFIG_ROOT_ENV_NAME):
        return _derived(CONFIG_ROOT_ENV_NAME, "config"), CONFIG_ROOT_ENV
    if _env(MEMORY_ROOT_ENV):
        return _derived(CONFIG_ROOT_ENV_NAME, "config"), CONFIG_ROOT_LEGACY
    return _derived(CONFIG_ROOT_ENV_NAME, "config"), CONFIG_ROOT_DEFAULT


def config_root() -> str:
    return _derived(CONFIG_ROOT_ENV_NAME, "config")


def engine_root() -> str:
    return _derived(ENGINE_ROOT_ENV, "engine")


def logs_root() -> str:
    """Logs are user state, not payload: never under the install root, where a
    reinstall deletes them."""
    return _derived(LOGS_ROOT_ENV, "logs")


def models_root() -> str:
    """GGUF weights m3 owns itself. Probed FIRST among model locations: every
    other one belongs to a tool the user may uninstall."""
    return _derived(MODELS_ROOT_ENV, "models")
