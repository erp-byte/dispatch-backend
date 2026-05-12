from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal

from services.sheets_service.hooks.base import HookContext


def _column_letter_to_index(letter: str) -> int:
    letter = letter.upper()
    idx = 0
    for ch in letter:
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"invalid column letter: {letter!r}")
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


class TimestampHook:
    """Inject the current time into a fixed column on append/update.

    NOTE: this hook mutates ``ctx.payload['values']`` rows in place.
    SheetsManager copies rows defensively before dispatching pre_* hooks
    (see manager.append_rows / update_range), so external callers are
    unaffected when the hook runs through the manager. If you invoke this
    hook directly (e.g. in tests or a custom pipeline), pass copies of any
    rows you need to retain unmodified.
    """
    name = "TimestampHook"

    def __init__(self, column: str, format: Literal["iso", "epoch"] = "iso"):
        self._index = _column_letter_to_index(column)
        self._format = format

    def _now(self) -> str | float:
        if self._format == "epoch":
            return time.time()
        return datetime.now(timezone.utc).isoformat()

    async def __call__(self, ctx: HookContext) -> None:
        if not ctx.payload:
            return
        rows = ctx.payload.get("values")
        if not rows:
            return
        ts = self._now()
        for row in rows:
            while len(row) <= self._index:
                row.append("")
            row[self._index] = ts
