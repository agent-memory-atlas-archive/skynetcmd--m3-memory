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
