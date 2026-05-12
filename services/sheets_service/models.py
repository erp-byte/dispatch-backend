from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --- list_tabs ---

class TabInfo(BaseModel):
    title: str
    sheet_id: int = Field(description="Numeric sheetId from Google Sheets API")
    grid_props: dict[str, Any] | None = None


class ListTabsRequest(BaseModel):
    spreadsheet_id: str = Field(description="The Google Sheets spreadsheet ID (from the URL).")


class ListTabsResponse(BaseModel):
    spreadsheet_id: str
    tabs: list[TabInfo]


# --- read_range ---

class ReadRangeRequest(BaseModel):
    spreadsheet_id: str
    range_a1: str = Field(description="A1 notation range, e.g. 'Sheet1!A1:D100'.")
    value_render: Literal["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"] | None = None


class ReadRangeResponse(BaseModel):
    range: str
    values: list[list[Any]] = Field(default_factory=list)


# --- append_rows ---

class AppendRowsRequest(BaseModel):
    spreadsheet_id: str
    range_a1: str
    rows: list[list[Any]]
    value_input_option: Literal["USER_ENTERED", "RAW"] | None = None


class AppendRowsResponse(BaseModel):
    updated_range: str | None = None
    updated_rows: int = 0
    updated_cells: int = 0


# --- update_range ---

class UpdateRangeRequest(BaseModel):
    spreadsheet_id: str
    range_a1: str
    values: list[list[Any]]
    value_input_option: Literal["USER_ENTERED", "RAW"] | None = None


class UpdateRangeResponse(BaseModel):
    updated_range: str | None = None
    updated_cells: int = 0


# --- clear_range ---

class ClearRangeRequest(BaseModel):
    spreadsheet_id: str
    range_a1: str


class ClearRangeResponse(BaseModel):
    cleared_range: str


# --- create_spreadsheet ---

class TabSpec(BaseModel):
    title: str
    rows: int | None = None
    cols: int | None = None


class CreateSpreadsheetRequest(BaseModel):
    title: str
    tabs: list[TabSpec] | None = None


class CreateSpreadsheetResponse(BaseModel):
    spreadsheet_id: str
    url: str


# --- add_tab ---

class AddTabRequest(BaseModel):
    spreadsheet_id: str
    title: str
    rows: int | None = None
    cols: int | None = None


class AddTabResponse(BaseModel):
    sheet_id: int
    title: str


# --- format_cells ---

class CellFormat(BaseModel):
    bold: bool | None = None
    bg_color: str | None = Field(default=None, description="Hex color like '#FFAA00'.")
    text_color: str | None = None
    number_format: str | None = Field(
        default=None, description="e.g. '#,##0.00' or 'yyyy-mm-dd'.")


class FormatCellsRequest(BaseModel):
    spreadsheet_id: str
    range_a1: str
    format: CellFormat


class FormatCellsResponse(BaseModel):
    replies: list[dict[str, Any]] = Field(default_factory=list)


# --- merge_cells ---

class MergeCellsRequest(BaseModel):
    spreadsheet_id: str
    range_a1: str
    merge_type: Literal["MERGE_ALL", "MERGE_COLUMNS", "MERGE_ROWS"] = "MERGE_ALL"


class MergeCellsResponse(BaseModel):
    merged_range: str


# --- unmerge_cells ---

class UnmergeCellsRequest(BaseModel):
    spreadsheet_id: str
    range_a1: str


class UnmergeCellsResponse(BaseModel):
    unmerged_range: str


# --- hide_columns / show_columns ---

class HideColumnsRequest(BaseModel):
    spreadsheet_id: str
    sheet_id: int | None = Field(default=None, description="Tab ID. If None, leftmost tab is used.")
    sheet_name: str | None = Field(default=None, description="Tab name. Either sheet_id or sheet_name must be set.")
    columns: list[int] = Field(description="1-indexed column numbers to hide.")
    hidden: bool = Field(default=True, description="False = unhide.")


class HideColumnsResponse(BaseModel):
    columns: list[int]
    hidden: bool


# --- delete_dimensions ---

class DeleteDimensionsRequest(BaseModel):
    spreadsheet_id: str
    sheet_id: int | None = Field(default=None, description="Tab ID; falls back to sheet_name.")
    sheet_name: str | None = Field(default=None, description="Tab name. Either sheet_id or sheet_name must be set.")
    dimension: Literal["ROWS", "COLUMNS"] = "ROWS"
    start_index: int = Field(description="1-indexed start of the range to delete.")
    end_index: int = Field(description="1-indexed inclusive end of the range to delete.")


class DeleteDimensionsResponse(BaseModel):
    dimension: str
    start_index: int
    end_index: int
