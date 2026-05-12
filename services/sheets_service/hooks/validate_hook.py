from __future__ import annotations

from typing import Any, Type

from pydantic import BaseModel, ValidationError

from services.sheets_service.hooks.base import HookContext
from shared.exceptions import HookAbort


class ValidateHook:
    name = "ValidateHook"

    def __init__(self) -> None:
        self._schemas: dict[str, Type[BaseModel]] = {}

    def register_schema(self, spreadsheet_id: str, schema: Type[BaseModel]) -> None:
        self._schemas[spreadsheet_id] = schema

    async def __call__(self, ctx: HookContext) -> None:
        schema = self._schemas.get(ctx.spreadsheet_id)
        if schema is None or not ctx.payload:
            return
        rows: list[list[Any]] = ctx.payload.get("values") or []
        field_order = list(schema.model_fields.keys())
        for i, row in enumerate(rows):
            data = {field_order[j]: row[j] for j in range(min(len(row), len(field_order)))}
            try:
                schema.model_validate(data)
            except ValidationError as e:
                errs = e.errors()
                detail = "; ".join(
                    f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
                    for err in errs
                )
                raise HookAbort(
                    reason=f"row {i} failed schema {schema.__name__}: {detail}",
                    hook_name=self.name,
                ) from e
