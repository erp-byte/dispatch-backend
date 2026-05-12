from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from services.sheets_service.manager import SheetsManager
from services.sheets_service.models import (
    AddTabRequest, AddTabResponse,
    AppendRowsRequest, AppendRowsResponse,
    ClearRangeRequest, ClearRangeResponse,
    CreateSpreadsheetRequest, CreateSpreadsheetResponse,
    DeleteDimensionsRequest, DeleteDimensionsResponse,
    FormatCellsRequest, FormatCellsResponse,
    HideColumnsRequest, HideColumnsResponse,
    ListTabsRequest, ListTabsResponse,
    MergeCellsRequest, MergeCellsResponse,
    ReadRangeRequest, ReadRangeResponse,
    UnmergeCellsRequest, UnmergeCellsResponse,
    UpdateRangeRequest, UpdateRangeResponse,
)


def register(server: FastMCP, mgr: SheetsManager) -> None:
    @server.tool()
    async def sheets_list_tabs(req: ListTabsRequest) -> ListTabsResponse:
        """List all tabs in a Google Sheets spreadsheet."""
        return await mgr.list_tabs(req)

    @server.tool()
    async def sheets_read_range(req: ReadRangeRequest) -> ReadRangeResponse:
        """Read values from an A1 range. Hooks: pre_read, post_read."""
        return await mgr.read_range(req)

    @server.tool()
    async def sheets_append_rows(req: AppendRowsRequest) -> AppendRowsResponse:
        """Append rows. Hooks: pre_append (may abort/mutate), post_append."""
        return await mgr.append_rows(req)

    @server.tool()
    async def sheets_update_range(req: UpdateRangeRequest) -> UpdateRangeResponse:
        """Update an A1 range. Hooks: pre_update, post_update."""
        return await mgr.update_range(req)

    @server.tool()
    async def sheets_clear_range(req: ClearRangeRequest) -> ClearRangeResponse:
        """Clear an A1 range. Hooks: pre_update, post_update."""
        return await mgr.clear_range(req)

    @server.tool()
    async def sheets_create_spreadsheet(req: CreateSpreadsheetRequest) -> CreateSpreadsheetResponse:
        """Create a new spreadsheet. Hooks: pre_create, post_create."""
        return await mgr.create_spreadsheet(req)

    @server.tool()
    async def sheets_add_tab(req: AddTabRequest) -> AddTabResponse:
        """Add a new tab to an existing spreadsheet. Hooks: pre_create, post_create."""
        return await mgr.add_tab(req)

    @server.tool()
    async def sheets_format_cells(req: FormatCellsRequest) -> FormatCellsResponse:
        """Format cells (bold/colors/number format). Hooks: pre_update, post_update."""
        return await mgr.format_cells(req)

    @server.tool()
    async def sheets_merge_cells(req: MergeCellsRequest) -> MergeCellsResponse:
        """Merge a rectangular range. Hooks: pre_update, post_update."""
        return await mgr.merge_cells(req)

    @server.tool()
    async def sheets_unmerge_cells(req: UnmergeCellsRequest) -> UnmergeCellsResponse:
        """Unmerge any merges intersecting the given range. Hooks: pre_update, post_update."""
        return await mgr.unmerge_cells(req)

    @server.tool()
    async def sheets_hide_columns(req: HideColumnsRequest) -> HideColumnsResponse:
        """Hide (or show) columns on a tab. Hooks: pre_update, post_update."""
        return await mgr.hide_columns(req)

    @server.tool()
    async def sheets_delete_dimensions(req: DeleteDimensionsRequest) -> DeleteDimensionsResponse:
        """Delete rows or columns on a tab. Hooks: pre_update, post_update."""
        return await mgr.delete_dimensions(req)
