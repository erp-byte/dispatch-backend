from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class HookEvent(str, Enum):
    PRE_READ = "pre_read"
    POST_READ = "post_read"
    PRE_APPEND = "pre_append"
    POST_APPEND = "post_append"
    PRE_UPDATE = "pre_update"
    POST_UPDATE = "post_update"
    PRE_CREATE = "pre_create"
    POST_CREATE = "post_create"
    ON_ERROR = "on_error"


@dataclass
class HookContext:
    event: HookEvent
    spreadsheet_id: str
    range: str | None = None
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: Exception | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Hook(Protocol):
    name: str

    async def __call__(self, ctx: HookContext) -> None: ...
