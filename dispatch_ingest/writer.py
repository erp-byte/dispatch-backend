"""Write per-article invoice rows to the live Google Sheet.

All writes route through ``SheetsManager`` so they participate in the project's
Validate / Audit / Timestamp hook chain (if enabled via env). Reads use the
same manager for consistency.

The DispatchWriter is async (the underlying manager is) — entry-point scripts
should run methods via ``asyncio.run()``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from dispatch_ingest.classifier import Route
from dispatch_ingest.rows import InvoiceRowBlock, SCHEMA_BY_ROUTE
from dispatch_ingest.utils import col_letter
from services.sheets_service.manager import SheetsManager
from services.sheets_service.models import (
    AppendRowsRequest,
    HideColumnsRequest,
    ListTabsRequest,
    MergeCellsRequest,
    ReadRangeRequest,
    UpdateRangeRequest,
)
from services.auth_service.client_factory import ClientFactory


_ARTICLE_HEADER = "Article"
_WAREHOUSE_HEADER = "Warehouse"

_RANGE_A1_RE = re.compile(
    r"^(?:'?(?P<tab>[^'!]+)'?!)?(?P<sc>[A-Z]+)(?P<sr>\d+):(?P<ec>[A-Z]+)(?P<er>\d+)$"
)


def _parse_updated_range(updated_range: str) -> tuple[int, int]:
    m = _RANGE_A1_RE.match(updated_range)
    if not m:
        raise ValueError(f"unparseable Sheets range: {updated_range!r}")
    return int(m.group("sr")), int(m.group("er"))


@dataclass
class TabRef:
    tab_id: int
    tab_name: str
    grid_rows: int
    grid_cols: int


class DispatchWriter:
    """Async writer that routes through SheetsManager for hook coverage.

    Pass `dry_run=True` to print intended operations without issuing any API
    calls. The underlying manager is shared between dry and live modes so
    behaviour is identical except for the final dispatch.
    """

    def __init__(self, manager: SheetsManager, factory: ClientFactory,
                 spreadsheet_id: str, dry_run: bool = False):
        self._mgr = manager
        # The factory is held for the small handful of metadata reads that
        # SheetsManager doesn't expose (sheet merges + column visibility).
        # All writes still go through the manager.
        self._factory = factory
        self._sid = spreadsheet_id
        self._dry = dry_run

    @property
    def dry_run(self) -> bool:
        return self._dry

    # ---------- tab discovery ----------

    async def list_tabs(self) -> dict[str, TabRef]:
        # Use the raw sheets API for one call — SheetsManager.list_tabs gives
        # us a thinner TabInfo that doesn't include grid_rows/cols.
        meta = self._factory.sheets().spreadsheets().get(
            spreadsheetId=self._sid, fields="sheets(properties)"
        ).execute()
        out: dict[str, TabRef] = {}
        for s in meta.get("sheets", []):
            p = s["properties"]
            gp = p.get("gridProperties", {})
            out[p["title"]] = TabRef(
                tab_id=p["sheetId"],
                tab_name=p["title"],
                grid_rows=gp.get("rowCount", 1000),
                grid_cols=gp.get("columnCount", 26),
            )
        return out

    def fetch_hidden_columns(self, tab: TabRef) -> set[int]:
        meta = self._factory.sheets().spreadsheets().get(
            spreadsheetId=self._sid,
            ranges=[f"'{tab.tab_name}'!A1:A1"],
            fields="sheets(properties(sheetId),data(columnMetadata(hiddenByUser)))",
        ).execute()
        hidden: set[int] = set()
        for sht in meta.get("sheets", []):
            if sht.get("properties", {}).get("sheetId") != tab.tab_id:
                continue
            data = sht.get("data", [])
            if not data:
                continue
            for i, cm in enumerate(data[0].get("columnMetadata", []), start=1):
                if cm.get("hiddenByUser"):
                    hidden.add(i)
        return hidden

    # ---------- header migration ----------

    async def ensure_named_column(self, route: Route, tab: TabRef, key: str, header: str) -> bool:
        """Idempotent: write `header` into row 1 of the column for `key` if blank."""
        _name, cols, _w = SCHEMA_BY_ROUTE[route]
        col = cols[key]
        if tab.grid_cols < col:
            if self._dry:
                print(f"  [dry] would extend {tab.tab_name} grid by "
                      f"{col - tab.grid_cols} col(s) to fit '{header}'")
            else:
                self._factory.sheets().spreadsheets().batchUpdate(
                    spreadsheetId=self._sid,
                    body={"requests": [{
                        "appendDimension": {
                            "sheetId": tab.tab_id,
                            "dimension": "COLUMNS",
                            "length": col - tab.grid_cols,
                        }
                    }]},
                ).execute()
                tab.grid_cols = col

        cell_range = f"'{tab.tab_name}'!{col_letter(col)}1"
        cur_resp = await self._mgr.read_range(ReadRangeRequest(
            spreadsheet_id=self._sid, range_a1=cell_range,
        ))
        cur = cur_resp.values
        if cur and cur[0] and str(cur[0][0]).strip():
            return False
        if self._dry:
            print(f"  [dry] would set {cell_range} = {header!r}")
            return True
        await self._mgr.update_range(UpdateRangeRequest(
            spreadsheet_id=self._sid, range_a1=cell_range,
            values=[[header]], value_input_option="RAW",
        ))
        return True

    async def ensure_article_column(self, route: Route, tab: TabRef) -> bool:
        return await self.ensure_named_column(route, tab, "ARTICLE", _ARTICLE_HEADER)

    async def ensure_warehouse_column(self, route: Route, tab: TabRef) -> bool:
        return await self.ensure_named_column(route, tab, "WAREHOUSE", _WAREHOUSE_HEADER)

    # ---------- dedup ----------

    async def fetch_existing_invoice_nos(self, route: Route, tab: TabRef) -> set[str]:
        _name, cols, _w = SCHEMA_BY_ROUTE[route]
        letter = col_letter(cols["INVOICE_NO"])
        rng = f"'{tab.tab_name}'!{letter}2:{letter}"
        resp = await self._mgr.read_range(ReadRangeRequest(
            spreadsheet_id=self._sid, range_a1=rng,
        ))
        out: set[str] = set()
        for row in resp.values:
            if row and row[0]:
                out.add(str(row[0]).strip())
        return out

    # ---------- write one invoice block ----------

    async def write_block(self, block: InvoiceRowBlock, tab: TabRef,
                          skip_merge_cols: set[int] | None = None
                          ) -> tuple[int, int]:
        """Append block.rows (race-safe), then center and merge merge_cols across them.

        Returns (start_row, end_row) inclusive.
        """
        skip_merge_cols = skip_merge_cols or set()
        _name, _cols, width = SCHEMA_BY_ROUTE[block.route]
        effective_merge_cols = [c for c in block.merge_cols if c not in skip_merge_cols]
        append_range = f"'{tab.tab_name}'!A:{col_letter(width)}"

        if self._dry:
            print(f"  [dry] would append {block.n_articles} row(s) × {width} cols "
                  f"to '{tab.tab_name}' for {block.invoice_no}")
            if effective_merge_cols:
                print(f"  [dry] would merge cols {[col_letter(c) for c in effective_merge_cols]} "
                      f"across the appended block")
            if skip_merge_cols:
                skipped = [col_letter(c) for c in skip_merge_cols if c in block.merge_cols]
                if skipped:
                    print(f"  [dry] deferring cols {skipped} for cross-invoice merge")
            return (-1, -1)

        resp = await self._mgr.append_rows(AppendRowsRequest(
            spreadsheet_id=self._sid, range_a1=append_range,
            rows=block.rows, value_input_option="USER_ENTERED",
        ))
        if not resp.updated_range:
            raise RuntimeError(f"append_rows returned no updated_range for {block.invoice_no!r}")
        start_row, end_row = _parse_updated_range(resp.updated_range)

        # Center the appended range
        await self._format_center(tab, start_row, end_row, 1, width)

        # Per-invoice column merges
        if block.n_articles > 1:
            for c in effective_merge_cols:
                rng = (f"'{tab.tab_name}'!{col_letter(c)}{start_row}:"
                       f"{col_letter(c)}{end_row}")
                await self._mgr.merge_cells(MergeCellsRequest(
                    spreadsheet_id=self._sid, range_a1=rng, merge_type="MERGE_ALL",
                ))
        return (start_row, end_row)

    # ---------- column visibility ----------

    async def hide_columns(self, tab: TabRef, cols: list[int], label: str = "") -> None:
        if not cols:
            return
        if self._dry:
            print(f"  [dry] would hide cols {[col_letter(c) for c in cols]}  ({label})")
            return
        already_hidden = self.fetch_hidden_columns(tab)
        to_hide = [c for c in cols if c not in already_hidden]
        if not to_hide:
            return
        await self._mgr.hide_columns(HideColumnsRequest(
            spreadsheet_id=self._sid, sheet_id=tab.tab_id,
            columns=to_hide, hidden=True,
        ))

    # ---------- formatting helpers ----------

    async def _format_center(self, tab: TabRef, start_row: int, end_row: int,
                             start_col: int, end_col: int) -> None:
        """Apply center+wrap formatting to a range. Uses raw API since SheetsManager.format_cells
        only takes the limited CellFormat shape (bold/color/number) — center isn't in the model."""
        if end_row < start_row:
            return
        if self._dry:
            print(f"  [dry] would center rows {start_row}..{end_row} cols "
                  f"{col_letter(start_col)}..{col_letter(end_col)}")
            return
        self._factory.sheets().spreadsheets().batchUpdate(
            spreadsheetId=self._sid,
            body={"requests": [{
                "repeatCell": {
                    "range": {
                        "sheetId": tab.tab_id,
                        "startRowIndex": start_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": start_col - 1,
                        "endColumnIndex": end_col,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,wrapStrategy)",
                }
            }]},
        ).execute()

    async def center_range(self, tab: TabRef, start_row: int, end_row: int,
                           start_col: int = 1, end_col: int | None = None,
                           label: str = "") -> None:
        end_col = end_col or tab.grid_cols
        await self._format_center(tab, start_row, end_row, start_col, end_col)

    async def merge_range(self, tab: TabRef, col: int, start_row: int, end_row: int,
                          label: str = "") -> None:
        if end_row <= start_row:
            return
        if self._dry:
            print(f"  [dry] would merge col {col_letter(col)} across rows "
                  f"{start_row}..{end_row}  ({label})")
            return
        rng = f"'{tab.tab_name}'!{col_letter(col)}{start_row}:{col_letter(col)}{end_row}"
        await self._mgr.merge_cells(MergeCellsRequest(
            spreadsheet_id=self._sid, range_a1=rng, merge_type="MERGE_ALL",
        ))


# Allow dots (legacy IDs) in addition to alphanumerics, hyphen, underscore (I18)
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_.\-]+)")


def extract_spreadsheet_id(url_or_id: str) -> str:
    m = _SHEET_ID_RE.search(url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()


def build_writer(spreadsheet_id: str, dry_run: bool = False) -> DispatchWriter:
    """Construct a DispatchWriter with a fresh SheetsManager + ClientFactory.

    Respects env-configured hooks (AuditHook etc.) per `register_default_hooks`,
    so enabling `SHEETS_AUDIT_ENABLED=true` in `.env` makes every batch ingest
    write get captured by the project's audit trail.
    """
    from shared.config_loader import load_config
    from services.auth_service.credentials import load_credentials
    from services.sheets_service.hook_registry import HookHub
    from services.sheets_service.hooks.registration import register_default_hooks

    config = load_config()
    creds = load_credentials(config)
    factory = ClientFactory(creds)
    hub = HookHub()
    register_default_hooks(hub, config)
    manager = SheetsManager(factory, hub, config.sheets)
    return DispatchWriter(manager, factory, spreadsheet_id, dry_run=dry_run)
