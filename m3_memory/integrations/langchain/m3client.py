"""Back-compat import path: the client moved to ``m3_memory.integrations._client``,
the one client every integration shares. Import from there in new code."""

from __future__ import annotations

from .._client import M3Client, _ensure_bin_on_path

__all__ = ["M3Client", "_ensure_bin_on_path"]
