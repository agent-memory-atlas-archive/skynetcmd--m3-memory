"""Autonomous cognitive-loop write toggles, resolved from a config FILE (not env).

The cognitive loop runs under launchd / systemd / schtasks — none of which
inherit the operator's shell environment (the shipped units carry no Environment
block). So an env var like ``M3_CONSOLIDATION_AUTO=1`` exported in a shell is
simply ABSENT in the daemon process, leaving every autonomous write pass
permanently dry-run. This mirrors the governor, which was migrated off env vars
to a config file for exactly this reason (DESIGN §3: a knob a headless daemon
must read belongs in a code-resolved config file, not an env var).

Precedence per flag: m3_config.toml [autonomy] > this file > the registry's
legacy env name (M3_CONSOLIDATION_AUTO, ...) > default (<dynamic> design Q2,
2026-10-13). A missing file falls through silently (expected before first seed);
a malformed file warns loudly rather than silently reverting.
"""

import json
import logging
import os
from pathlib import Path

from m3_core.config import settings as _m3cfg
from m3_core.paths import get_m3_config_root

logger = logging.getLogger("M3_SDK")

_AUTONOMY_CONFIG_NAME = ".cognitive_loop_config.json"

# The file's flags; each is the registry key ``autonomy.<flag>``.
_FLAGS = ("consolidation_auto", "chatlog_prune_auto")

_AUTONOMY_TEMPLATE_COMMENT = (
    "Cognitive-loop autonomous-write toggles. Read live by the cognitive loop "
    "(launchd/systemd/schtasks daemons do NOT inherit shell env, so these must "
    "live in a file, not an env var). Set a flag to true to let that pass write "
    "for real; false (default) keeps it dry-run. Edit and save; the next pass "
    "picks it up."
)


def autonomy_config_path() -> str:
    return os.path.join(get_m3_config_root(), _AUTONOMY_CONFIG_NAME)  # noqa: PTH118 — root-derived string: Path would re-separate a forward-slash root


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def autonomy_flag(config_key: str) -> bool:
    """Resolve a boolean autonomy flag: m3_config.toml > this file > the registry
    (legacy env name, then default).

    Read live (re-read each call — these gate infrequent background passes, so a
    per-pass stat+parse is negligible and there is no long-lived cache to stale).
    Never raises: a missing file falls through; a malformed file warns loudly (§3)
    and falls back rather than silently reverting.
    """
    name = f"autonomy.{config_key}"
    pinned = _m3cfg.file_value(name)
    if pinned is not None:
        return bool(pinned)
    path = autonomy_config_path()
    try:
        with Path(path).open(encoding="utf-8") as f:
            cfg = json.load(f) or {}
        if config_key in cfg:
            return _truthy(cfg[config_key])
    except FileNotFoundError:
        pass  # not yet seeded -> the registry is the intended fallback
    except Exception as e:
        logger.warning(
            "Cognitive-loop autonomy config %s is unreadable/malformed (%s) — "
            "falling back to %s from m3_config.toml / its default until fixed.",
            path,
            e,
            name,
        )
    return bool(_m3cfg.setting(name))


def ensure_autonomy_config() -> str:
    """Create ``<config_root>/.cognitive_loop_config.json`` with all flags at
    their CURRENT effective values if it does not already exist. Idempotent and
    race-safe (atomic create; never overwrites). Returns the path. Never raises —
    a write failure just leaves the loop on the registry's values. Call at setup and once
    at daemon startup; NOT from the per-pass read path.
    """
    path = autonomy_config_path()
    try:
        if os.path.exists(path):
            return path
        payload: dict[str, object] = {"_comment": _AUTONOMY_TEMPLATE_COMMENT}
        for key in _FLAGS:
            payload[key] = bool(_m3cfg.setting(f"autonomy.{key}"))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Atomic create: O_EXCL loses the race harmlessly if a peer seeds first.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            os.close(fd)
            raise
    except FileExistsError:
        pass  # a peer seeded it between the check and the create — fine
    except Exception as e:
        logger.warning("Could not seed autonomy config %s (%s) — loop stays on the registry values.", path, e)
    return path
