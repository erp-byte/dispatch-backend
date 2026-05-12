"""Small shared utilities used across the ingest pipeline."""

from __future__ import annotations

import io
import sys


def col_letter(n: int) -> str:
    """1-indexed column number → A1-notation letter."""
    if n <= 0:
        raise ValueError(f"col_letter() requires n > 0, got {n}")
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def inv_no_key(inv_no: str) -> tuple:
    """Sort key for invoice numbers.

    Handles both single-slash prefixes ('CND/26-27/744' → ('CND', 2627, 744))
    and multi-slash prefixes like SR/CF ('SR/CF/26-27/61' → ('SR/CF', 2627, 61)).
    The trailing numeric is always the serial; the year-pair is the part right
    before it; everything to the left is the prefix.
    """
    parts = inv_no.split("/")
    if len(parts) < 3:
        return (inv_no, 0, 0)
    prefix = "/".join(parts[:-2])
    try:
        year_part = int(parts[-2].replace("-", ""))
        serial = int(parts[-1])
        return (prefix, year_part, serial)
    except (ValueError, IndexError):
        return (prefix, 0, 0)


_STDOUT_PATCHED = False


def ensure_utf8_stdout() -> None:
    """Reopen stdout in UTF-8 on Windows so glyphs like × / ✓ don't crash.

    Idempotent — multiple module-load calls won't rewrap an already-wrapped
    stdout (which closes the previous wrapper and leaves stale references).
    """
    global _STDOUT_PATCHED
    if _STDOUT_PATCHED:
        return
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        if getattr(sys.stdout, "encoding", "").lower().replace("-", "") != "utf8":
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    _STDOUT_PATCHED = True
