"""DB prep / profile loading — _today, _load_profile_with_path."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from m3_core.config import settings as _m3cfg
from m3_core.paths import get_m3_backups_root
from slm_intent import (
    Profile,
    _parse_profile,
    load_profile,
)
from slm_intent import (
    invalidate_cache as invalidate_profile_cache,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PROFILE = _m3cfg.setting("enrich.profile")
BACKUP_DIR = Path(get_m3_backups_root()) / "enrich"


def _today() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def _load_profile_with_path(name: str | None, path: str | None) -> Profile:
    """Load a profile by name (config/slm/<name>.yaml) OR explicit path.

    --profile-path wins if both are set.
    """
    if path:
        pth = Path(path).expanduser().resolve()
        if not os.path.exists(pth):
            sys.exit(f"ERROR: profile path not found: {pth}. observed: no file at that path. inspect: --profile-path")
        # Reuse slm_intent's parser, which validates required keys.
        return _parse_profile(pth.stem, pth)
    invalidate_profile_cache()
    prof = load_profile(name or DEFAULT_PROFILE)
    if not prof:
        sys.exit(
            f"ERROR: profile {name!r} not found in config/slm/. "
            f"Use --profile-path /full/path.yaml, or copy "
            f"config/slm/enrich_custom_stub.yaml to make your own. "
            f"observed: no config/slm/{name}.yaml. inspect: --profile / --profile-path"
        )
    return prof

