"""Retrofit cross-invoice PO clustering on rows that are already on the sheet."""

from __future__ import annotations

import argparse
import asyncio

from dispatch_ingest.classifier import Route
from dispatch_ingest.rows import SCHEMA_BY_ROUTE
from dispatch_ingest.sheet_scan import InvoiceOnSheet, find_invoices_in_range
from dispatch_ingest.utils import col_letter, ensure_utf8_stdout
from dispatch_ingest.writer import build_writer, extract_spreadsheet_id
from services.sheets_service.models import UnmergeCellsRequest


ensure_utf8_stdout()


_TAB_BY_ROUTE = {
    Route.CFPL: "CFPL",
    Route.CDPL: "CDPL",
    Route.SERVICE_CFPL: "SERVICE CFPL",
}


def _clusters(invoices: list[InvoiceOnSheet]) -> list[tuple[list[InvoiceOnSheet], str]]:
    out: list[tuple[list[InvoiceOnSheet], str]] = []
    i = 0
    while i < len(invoices):
        po = invoices[i].po
        if not po:
            i += 1
            continue
        j = i + 1
        while j < len(invoices):
            same_po = invoices[j].po == po
            contiguous = invoices[j].start_row == invoices[j - 1].end_row + 1
            if not (same_po and contiguous):
                break
            j += 1
        if j - i >= 2:
            out.append((invoices[i:j], po))
        i = j
    return out


async def run_async(spreadsheet_id: str, live: bool, from_row: int) -> None:
    writer = build_writer(spreadsheet_id, dry_run=not live)
    sa_email = getattr(writer._factory.credentials, "service_account_email", "(unknown)")
    print(f"  authenticated as: {sa_email}")
    print(f"  spreadsheet id  : {spreadsheet_id}")
    print(f"  mode            : {'LIVE WRITE' if live else 'DRY RUN'}")
    print(f"  scan from row   : {from_row}")

    tabs = await writer.list_tabs()

    for route, tab_name in _TAB_BY_ROUTE.items():
        if tab_name not in tabs:
            print(f"  !! tab '{tab_name}' not on sheet, skipping")
            continue
        tab = tabs[tab_name]
        cols = SCHEMA_BY_ROUTE[route][1]
        inv_col = cols["INVOICE_NO"]
        po_col = cols["PO"]
        if from_row > tab.grid_rows:
            print(f"\n=== {tab_name} - only {tab.grid_rows} rows (from_row={from_row}), nothing to scan ===")
            continue
        last_row = tab.grid_rows
        print(f"\n=== {tab_name} (rows {from_row}..{last_row}, PO=col {col_letter(po_col)}) ===")

        invoices = find_invoices_in_range(
            writer._factory.sheets(), spreadsheet_id, tab_name, tab.tab_id,
            inv_col, po_col, from_row, last_row,
        )
        print(f"  found {len(invoices)} invoice(s) in scan window")

        clusters = _clusters(invoices)
        if not clusters:
            print("  no multi-invoice PO clusters found")
            continue

        for cluster, po in clusters:
            first = cluster[0].start_row
            last = cluster[-1].end_row
            inv_list = ", ".join(c.invoice_no for c in cluster)
            print(f"  cluster: PO '{po}' across {len(cluster)} invoices  "
                  f"({inv_list}) - rows {first}..{last}")
            rng = f"'{tab_name}'!{col_letter(po_col)}{first}:{col_letter(po_col)}{last}"
            if writer.dry_run:
                print(f"  [dry] would unmerge+merge col {col_letter(po_col)} rows {first}..{last}")
            else:
                # Unmerge any pre-existing per-invoice PO merges, then issue the cluster merge.
                await writer._mgr.unmerge_cells(UnmergeCellsRequest(
                    spreadsheet_id=spreadsheet_id, range_a1=rng,
                ))
                await writer.merge_range(tab, po_col, first, last,
                                         label=f"PO '{po}' across {len(cluster)} invoices")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sheet", help="Sheet URL or ID")
    ap.add_argument("--live", action="store_true",
                    help="Actually issue merges (default: dry-run)")
    ap.add_argument("--from-row", type=int, default=2,
                    help="First row to scan (default 2, skipping header)")
    args = ap.parse_args()
    asyncio.run(run_async(extract_spreadsheet_id(args.sheet),
                          live=args.live, from_row=args.from_row))


if __name__ == "__main__":
    main()
