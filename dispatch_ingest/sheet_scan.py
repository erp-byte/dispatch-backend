"""Sheet inspection helpers: walk an existing tab's rows and emit per-invoice ranges.

The hard part is that multi-article invoices store the Invoice No only on
the first row of a merged region — rows 2..N look blank from `values.get`.
We combine the col-E values with the sheet's merge metadata to recover the
true row ranges, which avoids the truncation bug that came from trusting
`values.get` alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dispatch_ingest.utils import col_letter


@dataclass
class InvoiceOnSheet:
    invoice_no: str
    po: str
    start_row: int
    end_row: int  # inclusive


def find_invoices_in_range(
    svc: Any,
    spreadsheet_id: str,
    tab_name: str,
    tab_id: int,
    inv_col: int,
    po_col: int,
    first_row: int,
    last_row: int,
) -> list[InvoiceOnSheet]:
    """Return one InvoiceOnSheet per non-empty Invoice No within [first_row, last_row].

    Strategy:
        1. Read values from the Invoice No and PO columns.
        2. Read the sheet's `merges` metadata once.
        3. For each invoice, its row span = either the merge that contains its
           start_row in the Invoice No column, or [start_row, next_start_row - 1].
        4. The final invoice's end_row is bounded by the larger of its merge
           range and the last data row in the inv_col scan.
    """
    left = min(inv_col, po_col)
    right = max(inv_col, po_col)
    rng = f"'{tab_name}'!{col_letter(left)}{first_row}:{col_letter(right)}{last_row}"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=rng, majorDimension="ROWS",
    ).execute()
    rows = resp.get("values", [])
    width = right - left + 1
    inv_offset = inv_col - left
    po_offset = po_col - left

    # Collect starts: row index → (invoice_no, po)
    starts: list[tuple[int, str, str]] = []
    for offset, row in enumerate(rows):
        padded = row + [""] * (width - len(row))
        inv = (padded[inv_offset] or "").strip()
        po = (padded[po_offset] or "").strip()
        if inv:
            starts.append((first_row + offset, inv, po))

    if not starts:
        return []

    # Fetch merges on the inv_col so we can find the end_row of each invoice block.
    merges_resp = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId),merges)",
    ).execute()
    merges_for_tab: list[dict] = []
    for sht in merges_resp.get("sheets", []):
        if sht.get("properties", {}).get("sheetId") == tab_id:
            for m in sht.get("merges", []):
                if (m.get("startColumnIndex") == inv_col - 1
                        and m.get("endColumnIndex") == inv_col):
                    merges_for_tab.append(m)
            break

    def merge_end_for(start_row: int) -> int | None:
        for m in merges_for_tab:
            if m["startRowIndex"] + 1 == start_row:  # API is 0-indexed exclusive-end
                return m["endRowIndex"]  # already exclusive-end → inclusive last row
        return None

    invoices: list[InvoiceOnSheet] = []
    for idx, (row_idx, inv, po) in enumerate(starts):
        merge_end = merge_end_for(row_idx)
        if idx + 1 < len(starts):
            next_start = starts[idx + 1][0]
            end_row = min(next_start - 1, merge_end) if merge_end else (next_start - 1)
        else:
            # last invoice: prefer the merge end if known, else the last scanned row
            last_data = first_row + len(rows) - 1 if rows else row_idx
            end_row = merge_end if merge_end else max(row_idx, last_data)
        invoices.append(InvoiceOnSheet(
            invoice_no=inv, po=po, start_row=row_idx, end_row=end_row,
        ))
    return invoices
