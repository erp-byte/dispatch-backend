from __future__ import annotations

import asyncio
from typing import Any

from googleapiclient.errors import HttpError

from services.auth_service.client_factory import ClientFactory
from services.sheets_service.hook_registry import HookHub
from services.sheets_service.hooks.base import HookContext, HookEvent
from services.sheets_service.models import (
    AddTabRequest, AddTabResponse,
    AppendRowsRequest, AppendRowsResponse,
    ClearRangeRequest, ClearRangeResponse,
    CreateSpreadsheetRequest, CreateSpreadsheetResponse,
    DeleteDimensionsRequest, DeleteDimensionsResponse,
    FormatCellsRequest, FormatCellsResponse,
    HideColumnsRequest, HideColumnsResponse,
    ListTabsRequest, ListTabsResponse, TabInfo,
    MergeCellsRequest, MergeCellsResponse,
    ReadRangeRequest, ReadRangeResponse,
    UnmergeCellsRequest, UnmergeCellsResponse,
    UpdateRangeRequest, UpdateRangeResponse,
)
from shared.config_loader import SheetsConfig
from shared.exceptions import HookAbort, SheetsError


def _flatten_field_paths(d: dict[str, Any], prefix: str) -> list[str]:
    """Return one dotted path per leaf in `d`. Used to build the granular
    field mask for spreadsheets.batchUpdate(repeatCell). A coarse mask like
    'userEnteredFormat.textFormat' would wipe adjacent textFormat properties
    when only setting bold; expanding to 'userEnteredFormat.textFormat.bold'
    preserves them."""
    paths: list[str] = []
    for k, v in d.items():
        path = f"{prefix}.{k}"
        if isinstance(v, dict):
            paths.extend(_flatten_field_paths(v, path))
        else:
            paths.append(path)
    return paths


def _hex_to_rgb(hex_color: str) -> dict[str, float]:
    h = hex_color.lstrip("#")
    # Accept 3-char shorthand (#FAB → #FFAABB) for ergonomics.
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        raise ValueError(f"invalid hex color: {hex_color!r}")
    try:
        return {
            "red": int(h[0:2], 16) / 255.0,
            "green": int(h[2:4], 16) / 255.0,
            "blue": int(h[4:6], 16) / 255.0,
        }
    except ValueError as e:
        raise ValueError(f"invalid hex color: {hex_color!r}") from e


def _parse_a1_to_grid_range(range_a1: str, sheet_id: int) -> dict[str, Any]:
    """Best-effort A1 → GridRange. Format: 'SheetName!A1:C3' or 'A1:C3'.

    Supports partial ranges:
      - column-only ('A:C') omits row indices (Sheets treats absence as all rows)
      - row-only ('1:3') omits column indices

    Mismatched partial ranges (e.g. 'A1:C' or '1:A2') are rejected as ambiguous.
    """
    rng = range_a1.split("!", 1)[-1]
    start, _, end = rng.partition(":")

    def col_to_idx(col: str) -> int:
        idx = 0
        for ch in col.upper():
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
        return idx - 1

    def split_cell(cell: str) -> tuple[int | None, int | None, bool, bool]:
        if not cell:
            raise ValueError(f"invalid A1 range: {range_a1!r}")
        i = 0
        while i < len(cell) and cell[i].isalpha():
            i += 1
        letters, digits = cell[:i], cell[i:]
        if not letters and not digits:
            raise ValueError(f"invalid A1 range: {range_a1!r}")
        col = col_to_idx(letters) if letters else None
        if digits:
            row = int(digits) - 1
        elif letters:
            # Sheets convention: 'A:A' starts at row 0 (entire column).
            row = 0
        else:
            row = None
        return col, row, bool(letters), bool(digits)

    sc, sr, s_has_letters, s_has_digits = split_cell(start)
    ec, er, e_has_letters, e_has_digits = split_cell(end or start)

    # Reject mismatched partial endpoints. 'A1:C' (rows-and-cols → col-only) or
    # '1:A2' (row-only → rows-and-cols) is ambiguous; Sheets cannot infer which
    # axis the caller meant, so fail loudly rather than silently truncating.
    if ":" in rng:
        s_full = s_has_letters and s_has_digits
        e_full = e_has_letters and e_has_digits
        s_col_only = s_has_letters and not s_has_digits
        e_col_only = e_has_letters and not e_has_digits
        s_row_only = s_has_digits and not s_has_letters
        e_row_only = e_has_digits and not e_has_letters
        if (s_full and (e_col_only or e_row_only)) or \
           (e_full and (s_col_only or s_row_only)) or \
           (s_col_only and e_row_only) or \
           (s_row_only and e_col_only):
            raise ValueError(f"ambiguous A1 range: {range_a1!r}")

    grid: dict[str, Any] = {"sheetId": sheet_id}
    # Column-only ranges (e.g. 'A:C') have no digits in either endpoint —
    # omit row indices so Sheets treats it as all rows. Row-only ranges
    # ('1:3') have no letters — omit column indices similarly.
    has_row_info = ":" in rng and (any(ch.isdigit() for ch in start) or
                                   any(ch.isdigit() for ch in (end or start)))
    has_col_info = ":" in rng and (any(ch.isalpha() for ch in start) or
                                   any(ch.isalpha() for ch in (end or start)))
    if not (":" in rng):
        # Single-cell range like 'A1' — keep both axes as before.
        has_row_info = sr is not None and er is not None
        has_col_info = sc is not None and ec is not None
    if has_row_info and sr is not None and er is not None:
        grid["startRowIndex"] = sr
        grid["endRowIndex"] = er + 1
    if has_col_info and sc is not None and ec is not None:
        grid["startColumnIndex"] = sc
        grid["endColumnIndex"] = ec + 1
    return grid


class SheetsManager:
    def __init__(self, factory: ClientFactory, hub: HookHub, config: SheetsConfig):
        self._factory = factory
        self._hub = hub
        self._config = config

    @property
    def _sa_email(self) -> str | None:
        return getattr(self._factory.credentials, "service_account_email", None)

    def _wrap_http(self, e: HttpError) -> SheetsError:
        return SheetsError.from_http(e, service_account_email=self._sa_email)

    async def _run(self, fn):
        return await asyncio.to_thread(fn)

    # ---------- list_tabs ----------

    async def list_tabs(self, req: ListTabsRequest) -> ListTabsResponse:
        try:
            data = await self._run(
                lambda: self._factory.sheets().spreadsheets()
                    .get(spreadsheetId=req.spreadsheet_id,
                         fields="sheets.properties").execute()
            )
        except HttpError as e:
            raise self._wrap_http(e)
        tabs = [
            TabInfo(
                title=s["properties"]["title"],
                sheet_id=s["properties"]["sheetId"],
                grid_props=s["properties"].get("gridProperties"),
            )
            for s in data.get("sheets", [])
        ]
        return ListTabsResponse(spreadsheet_id=req.spreadsheet_id, tabs=tabs)

    # ---------- read_range ----------

    async def read_range(self, req: ReadRangeRequest) -> ReadRangeResponse:
        ctx = HookContext(event=HookEvent.PRE_READ, spreadsheet_id=req.spreadsheet_id,
                          range=req.range_a1)
        try:
            await self._hub.dispatch(HookEvent.PRE_READ, ctx)
            render = req.value_render or self._config.default_value_render
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets().values()
                        .get(spreadsheetId=req.spreadsheet_id, range=req.range_a1,
                             valueRenderOption=render).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_READ, ctx)
            return ReadRangeResponse(range=data.get("range", req.range_a1),
                                     values=data.get("values", []))
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass  # ON_ERROR hook failures must not mask original abort
            raise

    # ---------- append_rows ----------

    async def append_rows(self, req: AppendRowsRequest) -> AppendRowsResponse:
        rows_copy = [list(r) for r in req.rows]
        ctx = HookContext(event=HookEvent.PRE_APPEND, spreadsheet_id=req.spreadsheet_id,
                          range=req.range_a1, payload={"values": rows_copy})
        try:
            await self._hub.dispatch(HookEvent.PRE_APPEND, ctx)
            vio = req.value_input_option or self._config.default_value_input
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets().values()
                        .append(spreadsheetId=req.spreadsheet_id, range=req.range_a1,
                                valueInputOption=vio,
                                body={"values": ctx.payload["values"]}).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_APPEND, ctx)
            updates = data.get("updates", {})
            return AppendRowsResponse(
                updated_range=updates.get("updatedRange"),
                updated_rows=updates.get("updatedRows", 0),
                updated_cells=updates.get("updatedCells", 0),
            )
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass  # ON_ERROR hook failures must not mask original abort
            raise

    # ---------- update_range ----------

    async def update_range(self, req: UpdateRangeRequest) -> UpdateRangeResponse:
        rows_copy = [list(r) for r in req.values]
        ctx = HookContext(event=HookEvent.PRE_UPDATE, spreadsheet_id=req.spreadsheet_id,
                          range=req.range_a1, payload={"values": rows_copy})
        try:
            await self._hub.dispatch(HookEvent.PRE_UPDATE, ctx)
            vio = req.value_input_option or self._config.default_value_input
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets().values()
                        .update(spreadsheetId=req.spreadsheet_id, range=req.range_a1,
                                valueInputOption=vio,
                                body={"values": ctx.payload["values"]}).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_UPDATE, ctx)
            return UpdateRangeResponse(
                updated_range=data.get("updatedRange"),
                updated_cells=data.get("updatedCells", 0),
            )
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass
            raise

    # ---------- clear_range ----------

    async def clear_range(self, req: ClearRangeRequest) -> ClearRangeResponse:
        ctx = HookContext(event=HookEvent.PRE_UPDATE, spreadsheet_id=req.spreadsheet_id,
                          range=req.range_a1, payload={})
        try:
            await self._hub.dispatch(HookEvent.PRE_UPDATE, ctx)
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets().values()
                        .clear(spreadsheetId=req.spreadsheet_id, range=req.range_a1,
                               body={}).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_UPDATE, ctx)
            return ClearRangeResponse(cleared_range=data.get("clearedRange", req.range_a1))
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass
            raise

    # ---------- create_spreadsheet ----------

    async def create_spreadsheet(self, req: CreateSpreadsheetRequest) -> CreateSpreadsheetResponse:
        body: dict[str, Any] = {"properties": {"title": req.title}}
        if req.tabs:
            body["sheets"] = [
                {"properties": {
                    "title": t.title,
                    "gridProperties": {
                        "rowCount": t.rows or 1000,
                        "columnCount": t.cols or 26,
                    },
                }} for t in req.tabs
            ]
        ctx = HookContext(event=HookEvent.PRE_CREATE, spreadsheet_id="",
                          payload={"title": req.title})
        try:
            await self._hub.dispatch(HookEvent.PRE_CREATE, ctx)
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets().create(body=body).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.spreadsheet_id = data["spreadsheetId"]
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_CREATE, ctx)
            return CreateSpreadsheetResponse(
                spreadsheet_id=data["spreadsheetId"],
                url=data.get("spreadsheetUrl",
                             f"https://docs.google.com/spreadsheets/d/{data['spreadsheetId']}"),
            )
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass
            raise

    # ---------- add_tab ----------

    async def add_tab(self, req: AddTabRequest) -> AddTabResponse:
        props: dict[str, Any] = {"title": req.title}
        if req.rows or req.cols:
            # Partial spec (e.g. only req.rows set) gets Sheets' usual defaults
            # for the unspecified axis (1000 rows / 26 cols).
            props["gridProperties"] = {
                "rowCount": req.rows or 1000,
                "columnCount": req.cols or 26,
            }
        body = {"requests": [{"addSheet": {"properties": props}}]}
        ctx = HookContext(event=HookEvent.PRE_CREATE, spreadsheet_id=req.spreadsheet_id,
                          payload=body)
        try:
            await self._hub.dispatch(HookEvent.PRE_CREATE, ctx)
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets()
                        .batchUpdate(spreadsheetId=req.spreadsheet_id, body=body).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_CREATE, ctx)
            added = data["replies"][0]["addSheet"]["properties"]
            return AddTabResponse(sheet_id=added["sheetId"], title=added["title"])
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass
            raise

    # ---------- format_cells ----------

    async def format_cells(self, req: FormatCellsRequest) -> FormatCellsResponse:
        # We need the sheetId — fetch tabs and resolve by name in range prefix.
        if "!" in req.range_a1:
            sheet_name = req.range_a1.split("!", 1)[0]
            tabs = await self.list_tabs(ListTabsRequest(spreadsheet_id=req.spreadsheet_id))
            found = next((t.sheet_id for t in tabs.tabs if t.title == sheet_name), None)
            if found is None:
                # Tab name typo — fail loudly. Silently formatting sheet 0
                # destroys data on the wrong tab.
                raise SheetsError(
                    code="TAB_NOT_FOUND", status=404, reason="Not Found",
                    message=f"tab named {sheet_name!r} does not exist in spreadsheet",
                    service_account_email=self._sa_email,
                    hint=f"Available tabs: {[t.title for t in tabs.tabs]}",
                )
            sheet_id = found
        else:
            # No sheet prefix: resolve to the actual leftmost (first) tab's id
            # rather than assuming sheetId=0, which may not exist if the first
            # tab was renamed/deleted.
            tabs = await self.list_tabs(ListTabsRequest(spreadsheet_id=req.spreadsheet_id))
            sheet_id = tabs.tabs[0].sheet_id if tabs.tabs else 0

        grid = _parse_a1_to_grid_range(req.range_a1, sheet_id)
        cell_format: dict[str, Any] = {}
        text_format: dict[str, Any] = {}
        if req.format.bold is not None:
            text_format["bold"] = req.format.bold
        if req.format.text_color:
            text_format["foregroundColor"] = _hex_to_rgb(req.format.text_color)
        if text_format:
            cell_format["textFormat"] = text_format
        if req.format.bg_color:
            cell_format["backgroundColor"] = _hex_to_rgb(req.format.bg_color)
        if req.format.number_format:
            cell_format["numberFormat"] = {"type": "NUMBER", "pattern": req.format.number_format}

        field_paths = (
            _flatten_field_paths(cell_format, "userEnteredFormat")
            if cell_format else ["userEnteredFormat"]
        )
        fields = ",".join(field_paths)
        body = {
            "requests": [{
                "repeatCell": {
                    "range": grid,
                    "cell": {"userEnteredFormat": cell_format},
                    "fields": fields,
                }
            }]
        }
        ctx = HookContext(event=HookEvent.PRE_UPDATE, spreadsheet_id=req.spreadsheet_id,
                          range=req.range_a1, payload=body)
        try:
            await self._hub.dispatch(HookEvent.PRE_UPDATE, ctx)
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets()
                        .batchUpdate(spreadsheetId=req.spreadsheet_id, body=body).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_UPDATE, ctx)
            return FormatCellsResponse(replies=data.get("replies", []))
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass
            raise

    # ---------- merge_cells / unmerge_cells ----------

    async def _resolve_sheet_id(self, spreadsheet_id: str, range_a1: str) -> int:
        """Resolve the sheetId for a range that may or may not have a tab prefix."""
        if "!" in range_a1:
            sheet_name = range_a1.split("!", 1)[0].strip("'")
            tabs = await self.list_tabs(ListTabsRequest(spreadsheet_id=spreadsheet_id))
            found = next((t.sheet_id for t in tabs.tabs if t.title == sheet_name), None)
            if found is None:
                raise SheetsError(
                    code="TAB_NOT_FOUND", status=404, reason="Not Found",
                    message=f"tab named {sheet_name!r} does not exist in spreadsheet",
                    service_account_email=self._sa_email,
                    hint=f"Available tabs: {[t.title for t in tabs.tabs]}",
                )
            return found
        tabs = await self.list_tabs(ListTabsRequest(spreadsheet_id=spreadsheet_id))
        return tabs.tabs[0].sheet_id if tabs.tabs else 0

    async def merge_cells(self, req: MergeCellsRequest) -> MergeCellsResponse:
        """Merge a range. Routes through PRE_UPDATE/POST_UPDATE hooks for audit."""
        sheet_id = await self._resolve_sheet_id(req.spreadsheet_id, req.range_a1)
        grid = _parse_a1_to_grid_range(req.range_a1, sheet_id)
        body = {"requests": [{"mergeCells": {"range": grid, "mergeType": req.merge_type}}]}
        ctx = HookContext(event=HookEvent.PRE_UPDATE, spreadsheet_id=req.spreadsheet_id,
                          range=req.range_a1, payload=body)
        try:
            await self._hub.dispatch(HookEvent.PRE_UPDATE, ctx)
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets()
                        .batchUpdate(spreadsheetId=req.spreadsheet_id, body=body).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_UPDATE, ctx)
            return MergeCellsResponse(merged_range=req.range_a1)
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass
            raise

    async def unmerge_cells(self, req: UnmergeCellsRequest) -> UnmergeCellsResponse:
        """Unmerge a range. Routes through PRE_UPDATE/POST_UPDATE hooks for audit."""
        sheet_id = await self._resolve_sheet_id(req.spreadsheet_id, req.range_a1)
        grid = _parse_a1_to_grid_range(req.range_a1, sheet_id)
        body = {"requests": [{"unmergeCells": {"range": grid}}]}
        ctx = HookContext(event=HookEvent.PRE_UPDATE, spreadsheet_id=req.spreadsheet_id,
                          range=req.range_a1, payload=body)
        try:
            await self._hub.dispatch(HookEvent.PRE_UPDATE, ctx)
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets()
                        .batchUpdate(spreadsheetId=req.spreadsheet_id, body=body).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_UPDATE, ctx)
            return UnmergeCellsResponse(unmerged_range=req.range_a1)
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass
            raise

    # ---------- hide_columns ----------

    async def hide_columns(self, req: HideColumnsRequest) -> HideColumnsResponse:
        """Hide (or show) one or more columns. Routes through PRE_UPDATE/POST_UPDATE hooks."""
        if req.sheet_id is not None:
            sheet_id = req.sheet_id
        elif req.sheet_name:
            tabs = await self.list_tabs(ListTabsRequest(spreadsheet_id=req.spreadsheet_id))
            found = next((t.sheet_id for t in tabs.tabs if t.title == req.sheet_name), None)
            if found is None:
                raise SheetsError(
                    code="TAB_NOT_FOUND", status=404, reason="Not Found",
                    message=f"tab named {req.sheet_name!r} does not exist in spreadsheet",
                    service_account_email=self._sa_email,
                    hint=f"Available tabs: {[t.title for t in tabs.tabs]}",
                )
            sheet_id = found
        else:
            tabs = await self.list_tabs(ListTabsRequest(spreadsheet_id=req.spreadsheet_id))
            sheet_id = tabs.tabs[0].sheet_id if tabs.tabs else 0

        requests = [{
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": c - 1,
                    "endIndex": c,
                },
                "properties": {"hiddenByUser": req.hidden},
                "fields": "hiddenByUser",
            }
        } for c in req.columns]
        body = {"requests": requests}
        ctx = HookContext(event=HookEvent.PRE_UPDATE, spreadsheet_id=req.spreadsheet_id,
                          range=None, payload=body)
        try:
            await self._hub.dispatch(HookEvent.PRE_UPDATE, ctx)
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets()
                        .batchUpdate(spreadsheetId=req.spreadsheet_id, body=body).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_UPDATE, ctx)
            return HideColumnsResponse(columns=req.columns, hidden=req.hidden)
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass
            raise

    # ---------- delete_dimensions ----------

    async def delete_dimensions(self, req: DeleteDimensionsRequest) -> DeleteDimensionsResponse:
        """Delete rows or columns. Routes through PRE/POST_UPDATE hooks + asyncio.to_thread."""
        if req.sheet_id is not None:
            sheet_id = req.sheet_id
        elif req.sheet_name:
            tabs = await self.list_tabs(ListTabsRequest(spreadsheet_id=req.spreadsheet_id))
            found = next((t.sheet_id for t in tabs.tabs if t.title == req.sheet_name), None)
            if found is None:
                raise SheetsError(
                    code="TAB_NOT_FOUND", status=404, reason="Not Found",
                    message=f"tab named {req.sheet_name!r} does not exist in spreadsheet",
                    service_account_email=self._sa_email,
                    hint=f"Available tabs: {[t.title for t in tabs.tabs]}",
                )
            sheet_id = found
        else:
            raise SheetsError(
                code="MISSING_SHEET", status=400, reason="Bad Request",
                message="delete_dimensions requires either sheet_id or sheet_name",
                service_account_email=self._sa_email, hint=None,
            )

        if req.end_index < req.start_index:
            raise SheetsError(
                code="OUT_OF_RANGE", status=400, reason="Bad Request",
                message=f"end_index {req.end_index} < start_index {req.start_index}",
                service_account_email=self._sa_email, hint=None,
            )

        body = {"requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": req.dimension,
                    "startIndex": req.start_index - 1,
                    "endIndex": req.end_index,
                }
            }
        }]}
        ctx = HookContext(event=HookEvent.PRE_UPDATE, spreadsheet_id=req.spreadsheet_id,
                          range=None, payload=body)
        try:
            await self._hub.dispatch(HookEvent.PRE_UPDATE, ctx)
            try:
                data = await self._run(
                    lambda: self._factory.sheets().spreadsheets()
                        .batchUpdate(spreadsheetId=req.spreadsheet_id, body=body).execute()
                )
            except HttpError as e:
                ctx.error = e
                await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                raise self._wrap_http(e)
            ctx.result = data
            await self._hub.dispatch(HookEvent.POST_UPDATE, ctx)
            return DeleteDimensionsResponse(
                dimension=req.dimension,
                start_index=req.start_index, end_index=req.end_index,
            )
        except HookAbort as e:
            if ctx.error is None:
                ctx.error = e
                try:
                    await self._hub.dispatch(HookEvent.ON_ERROR, ctx)
                except Exception:
                    pass
            raise
