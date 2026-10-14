"""The environment a child process starts with: the ONE owner (CONFIG_DESIGN §5,
plan Step 2E.1; env read path 7).

    child_env()                                 the parent's environment, copied
    child_env(PYTHONUTF8="1")                   plus a non-m3 override
    child_env(prepend_pythonpath=bin_dir)       bin/ first on the child's PYTHONPATH
    child_env(drop=names)                       without these names (secrets for a
                                                non-m3 child, or PYTHONPATH)

Each subprocess site used to build this dict itself, about twenty of them, with
`os.environ.copy()`, `dict(os.environ)` or `{**os.environ, ...}`. The overrides
are typed (PEP 692, ``**overrides: Unpack[ChildEnv]``), so a site cannot slip an m3
setting in as an env override: m3 settings reach an m3 child as CLI arguments
(`spawn_m3_child`, 2E.1 part 2), never as env, or the child would ignore them
after the cutover and silently use its defaults.

The parent's os.environ is never modified. Stdlib-only, beside _roots/_bin, so
the installed package and bin/ (through m3_core.seams) share it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypedDict, Unpack, cast


class ChildEnv(TypedDict, total=False):
    """The non-m3 names a child may be given. Add a name here, with its reason,
    rather than splatting os.environ at a call site."""

    PYTHONUTF8: str  # UTF-8 stdio for Python children (Windows consoles)
    PYTHONPATH: str  # replace the child's import path outright
    GGML_LOG_LEVEL: str  # quiet llama.cpp's own logging
    PGPASSWORD: str  # libpq tools (pg_dump / pg_restore): never on argv
    PIP_NO_CACHE_DIR: str  # pip without its cache (upgrade path)
    UV_NO_CACHE: str  # uv without its cache (upgrade path)
    PYTEST_DISABLE_PLUGIN_AUTOLOAD: str  # isolated pytest runs (drift gate)


@dataclass(frozen=True, slots=True)
class Handoff:
    """A SANCTIONED way to hand m3 values to a child through env.
    Everything else reaches an m3 child as
    CLI arguments. Each handoff names the only m3 variables it may carry, and why
    env is the one channel; tests/test_child_env.py pins the list."""

    name: str
    names: frozenset[str]
    why: str


RUST_EMBED_SERVER = Handoff(
    "rust-embed-server",
    frozenset({"M3_EMBED_GGUF", "M3_EMBED_SERVER_PORT"}),
    "m3-embed-server (m3-core-rs) is configured only through env, also as an OS service; until it reads m3_config.toml",
)
HANDOFFS: tuple[Handoff, ...] = (RUST_EMBED_SERVER,)


def child_env(
    *,
    drop: Iterable[str] = (),
    prepend_pythonpath: str | None = None,
    handoff: tuple[Handoff, Mapping[str, str]] | None = None,
    **overrides: Unpack[ChildEnv],
) -> dict[str, str]:
    """A NEW dict: the parent's environment minus ``drop``, with ``overrides``
    applied, ``handoff`` values set and ``prepend_pythonpath`` put first on
    PYTHONPATH. A handoff value whose name its Handoff does not list raises
    ValueError: an m3 setting slipped into a child's env must be reviewed, not
    silently passed."""
    dropped = set(drop)
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env.update(cast("dict[str, str]", overrides))  # every ChildEnv field is a str
    if handoff is not None:
        sanction, values = handoff
        stray = sorted(set(values) - sanction.names)
        if stray:
            raise ValueError(
                f"handoff {sanction.name!r} does not sanction {stray}; it carries {sorted(sanction.names)}"
            )
        env.update(values)
    if prepend_pythonpath:
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = prepend_pythonpath + (os.pathsep + current if current else "")
    return env
