from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from services.sheets_service.hooks.base import HookContext
from shared.logger import get_logger


class AuditHook:
    name = "AuditHook"

    def __init__(self, file_path: Path | None = None):
        if file_path is not None:
            resolved = Path(file_path).resolve()
            if not resolved.parent.is_dir():
                raise ValueError(
                    "audit file's parent directory does not exist or is not a dir: "
                    f"{resolved.parent}"
                )
            self._file_path: Path | None = resolved
        else:
            self._file_path = None
        self._log = get_logger("sheets.audit")

    def _append_line(self, line: str) -> None:
        with open(self._file_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def __call__(self, ctx: HookContext) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": ctx.event.value,
            "spreadsheet_id": ctx.spreadsheet_id,
            "range": ctx.range,
            "rows_updated": (ctx.result or {}).get("updatedRows"),
            "error": str(ctx.error) if ctx.error else None,
        }
        line = json.dumps(record, default=str)
        self._log.info(line)
        if self._file_path:
            try:
                await asyncio.to_thread(self._append_line, line)
            except OSError as e:
                # Audit-write failures must not propagate — they would
                # mis-attribute a successful sheet operation as failed.
                self._log.warning("audit write failed: %s", e)
