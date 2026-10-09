"""The config package's ONE read of the process environment (CONFIG_DESIGN §2,
read path 6). ``check_env_access`` allows this module and no other in the
package; everything else receives the mapping as an argument, so a test passes
its own and never patches ``os.environ``.
"""
from __future__ import annotations

import os
from collections.abc import Mapping


def environ() -> Mapping[str, str]:
    """The live process environment (read, never written)."""
    return os.environ


def dotenv() -> tuple[str | None, Mapping[str, str]]:
    """The legacy .env as (path, values), parsed WITHOUT injecting it into
    os.environ (CONFIG_DESIGN §10, #D20); (None, {}) when there is none. A
    name with no value (``FOO`` alone) is dropped: it sets nothing."""
    from m3_core.paths import dotenv_path

    path = dotenv_path()
    if path is None:
        return None, {}
    from dotenv import dotenv_values

    return path, {k: v for k, v in dotenv_values(path).items() if v is not None}
