"""Hidden and control characters in text m3 stores. THE owner of which
characters those are and what happens to them.

Stored text is shown in terminals and dashboards and read by agents and the
enrichment model, so a character the reader cannot see is a channel: terminal
escapes (Cc) recolour or rewrite a screen, bidi overrides (Cf) reorder what a
reviewer reads, and Unicode tag characters (Cf, U+E0000-E007F) spell an
instruction a model reads and a person does not. Measured 2026-10-18: all of
these were stored, and a hidden "ignore previous instructions" in tag characters
passed the prompt-injection check.

Forbidden, by Unicode general category (so new code points are classified by
the Unicode tables, not by a list someone must keep):
  - Cc (controls) except tab, newline, carriage return;
  - Cf (format) except ZWJ and ZWNJ (emoji sequences, Indic and Persian
    shaping) and LRM, RLM, ALM (the marks that keep mixed-direction text
    readable).

Consequence worth knowing: a subdivision flag (England, Scotland, Wales) is
spelled with tag characters and degrades to a plain black flag.

Writes keep the CLEANED text and record what was removed (``removal_record``):
code points and positions, the Unicode version that classified them, and a
hash of the original. The original itself is never stored, so it can never be
served back.
"""

from __future__ import annotations

import hashlib
import unicodedata

_ALLOWED_CC = frozenset("\t\n\r")
_ALLOWED_CF = frozenset("\u200c\u200d\u200e\u200f\u061c")  # ZWNJ ZWJ LRM RLM ALM
# Positions listed in a removal record; the count and hash cover the rest.
MAX_LISTED = 200


def is_forbidden(ch: str) -> bool:
    """Whether one character is a hidden or control character m3 removes."""
    cat = unicodedata.category(ch)
    if cat == "Cc":
        return ch not in _ALLOWED_CC
    if cat == "Cf":
        return ch not in _ALLOWED_CF
    return False


def clean(text: str) -> tuple[str, list[tuple[int, int]]]:
    """``text`` without forbidden characters, and what was removed as
    ``(position in the original, code point)`` pairs. Text with none comes back
    as the same object and an empty list."""
    if not text or (text.isascii() and text.isprintable()):
        return text, []
    removed = [(i, ord(ch)) for i, ch in enumerate(text) if is_forbidden(ch)]
    if not removed:
        return text, []
    return "".join(ch for ch in text if not is_forbidden(ch)), removed


def removal_record(original: str, removed: list[tuple[int, int]]) -> dict:
    """What a write records about removed characters, never the characters'
    surroundings: positions and code points (the first MAX_LISTED), how many,
    the Unicode version that classified them, and the original's SHA-256."""
    return {
        "removed": [{"pos": pos, "cp": f"U+{cp:04X}"} for pos, cp in removed[:MAX_LISTED]],
        "count": len(removed),
        "unicode_version": unicodedata.unidata_version,
        "original_sha256": hashlib.sha256(original.encode("utf-8", "surrogatepass"), usedforsecurity=False).hexdigest(),
    }


def summary(removed: list[tuple[int, int]]) -> str:
    """A short phrase for an agent: how many were removed and which code points,
    e.g. ``3 hidden/control characters removed (U+202E, U+E0049, ...)``."""
    seen: list[str] = []
    for _pos, cp in removed:
        tag = f"U+{cp:04X}"
        if tag not in seen:
            seen.append(tag)
    more = ", ..." if len(seen) > 5 else ""
    noun = "character" if len(removed) == 1 else "characters"
    return f"{len(removed)} hidden/control {noun} removed ({', '.join(seen[:5])}{more})"


def has_visible_text(text: str | None) -> bool:
    """Whether ``text`` holds anything a reader sees: a character outside the
    separators (Z*), the controls and format characters (C*) and the combining
    marks (M*, which show only on a base character)."""
    return any(not unicodedata.category(ch).startswith(("Z", "C", "M")) for ch in text or "")


def escape_for_display(text: str) -> str:
    """``text`` with each forbidden character shown as ``\\u{XXXX}``: the
    display-side guard for text that reached the store before writes cleaned
    it, or by a path that does not."""
    if not text or (text.isascii() and text.isprintable()):
        return text
    return "".join(f"\\u{{{ord(ch):04X}}}" if is_forbidden(ch) else ch for ch in text)
