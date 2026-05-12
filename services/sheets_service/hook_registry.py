from __future__ import annotations

from collections import defaultdict
from typing import Any

from services.sheets_service.hooks.base import Hook, HookContext, HookEvent
from shared.exceptions import HookAbort
from shared.logger import get_logger

log = get_logger("sheets.hooks")


class HookHub:
    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[Hook]] = defaultdict(list)

    def register(self, event: HookEvent, hook: Hook) -> None:
        self._hooks[event].append(hook)

    def hooks_for(self, event: HookEvent) -> list[Hook]:
        return list(self._hooks.get(event, ()))

    async def dispatch(self, event: HookEvent, ctx: HookContext) -> None:
        ctx.event = event
        for hook in self._hooks.get(event, ()):
            try:
                await hook(ctx)
            except HookAbort:
                # Caller's manager handles ON_ERROR dispatch for HookAbort.
                raise
            except Exception as e:
                log.warning(
                    "hook %r raised during %s: %s",
                    getattr(hook, "name", type(hook).__name__),
                    event.value,
                    e,
                )
                if event != HookEvent.ON_ERROR and ctx.error is None:
                    ctx.error = e
                    ctx.event = HookEvent.ON_ERROR
                    try:
                        for err_hook in self._hooks.get(HookEvent.ON_ERROR, ()):
                            await err_hook(ctx)
                    except Exception:
                        # Best-effort: don't mask the original exception.
                        pass
                raise
