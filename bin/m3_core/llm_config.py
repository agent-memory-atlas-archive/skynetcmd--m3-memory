"""LLM endpoint switches: where setup and install record them, and the legacy
``.llm_config.json``.

Setup and ``m3 install --llm-endpoint`` record a switch in m3_config.toml [llm]
(``write_llm_setting``), which every launcher reads, including launchd and systemd
jobs that start with a bare environment. ``.llm_config.json`` is no longer a value
source: the registry records what it sets as not read (``Resolved.shadowed``), and
import-env copies it when no m3_config.toml exists. This module keeps that file's
path and parse for those two readers.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from m3_core.paths import get_m3_config_root

logger = logging.getLogger("M3_SDK")

LLM_CONFIG_NAME = ".llm_config.json"


def llm_config_path() -> str:
    return os.path.join(get_m3_config_root(), LLM_CONFIG_NAME)  # noqa: PTH118 — root-derived string: Path would re-separate a forward-slash root


def read_llm_config() -> dict:
    """The file's settings, or {} when absent. A malformed file is reported,
    because the daemon would otherwise run without the user's switches."""
    path = llm_config_path()
    try:
        with Path(path).open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning(
            "observed: %s is unreadable (%s: %s); its LLM endpoint settings are "
            "ignored, so m3_config.toml [llm], the environment and .env answer, "
            "else the defaults. inspect: fix that file, or move its values into [llm]",
            path,
            type(e).__name__,
            e,
        )
        return {}
    return data if isinstance(data, dict) else {}


def write_llm_setting(name: str, value: str) -> str:
    """Record the LLM switch known by the former env name ``name`` (e.g.
    ``M3_ENABLE_OLLAMA_FAILOVER``) as its registry key in m3_config.toml, parsed as
    import-env parses it. Returns the file's path. An unknown name raises."""
    from m3_core.config import locate
    from m3_core.config.keys import REGISTRY
    from m3_core.config.writer import set_values

    key = REGISTRY.by_env(name)
    if key is None:
        raise ValueError(f"{name} is not the former name of an m3 setting")
    return str(set_values(locate(), {key: key.coerce_env_value(value)}))
