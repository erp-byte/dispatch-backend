"""ONE-SHOT (May 9, 2026) — retrofit the 09-May batch with all new column data.

Preserved for audit purposes after its single live run. Re-running it:
    1. The actual invoice numbers in the configured row ranges must match the
       parsed zip's invoice numbers (validated before any write).
    2. Default mode is dry-run. Use --live to actually write.

If row positions have shifted (anyone manually inserted rows above), the
validation step refuses to write and prints the mismatch.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import zipfile

from dispatch_ingest.classifier import classify, Route
from dispatch_ingest.parser import parse_pdf, InvoiceData
from dispatch_ingest.rows import SCHEMA_BY_ROUTE, NEW_COLUMN_HEADERS, HIDDEN_KEYS
from dispatch_ingest.sheet_scan import find_invoices_in_range, InvoiceOnSheet
from dispatch_ingest.utils import col_letter, ensure_utf8_stdout
from dispatch_ingest.writer import build_writer, extract_spreadsheet_id


ensure_utf8_stdout()


MAY_9_2026_ROW_RANGES = [
    (Route.CFPL, "CFPL", 10895, 10926),
    (Route.SERVICE_CFPL, "SERVICE CFPL", 1075, 1076),
]


def parse_all(zip_path: str) -> dict[str, InvoiceData]:
    out: dict[str, InvoiceData] = {}
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                if not name.lower().endswith(".pdf"):
                    continue
                if classify(name).route == Route.SKIP:
                    continue
                local = os.path.join(td, os.path.basename(name))
                with z.open(name) as src, open(local, "wb") as dst:
                    dst.write(src.read())
                inv = parse_pdf(local)
                if inv.invoice_no:
                    out[inv.invoice_no] = inv
    return out


def _validate_alignment(
    invs_on_sheet: list[InvoiceOnSheet],
    invoices_by_no: dict[str, InvoiceData],
    tab_name: str,
) -> tuple[bool, list[str]]:
    problems: list[str] = []
    for io_obj in invs_on_sheet:
        if io_obj.invoice_no not in invoices_by_no:
            problems.append(
                f"{tab_name} row {io_obj.start_row}..{io_obj.end_row}: "
                f"on-sheet Invoice No {io_obj.invoice_no!r} not present in the zip"
            )
    return (len(problems) == 0, problems)


async def patch_tab(writer, route: Route, tab_name: str, first_row: int, last_row: int,
                    invoices_by_no: dict[str, InvoiceData]) -> bool:
    tabs = await writer.list_tabs()
    if tab_name not in tabs:
        print(f"  !! tab '{tab_name}' not found, skipping")
        return False
    tab = tabs[tab_name]
    cols = SCHEMA_BY_ROUTE[route][1]

    print(f"\n=== {tab_name} ({first_row}..{last_row}) ===")
    for key, label in NEW_COLUMN_HEADERS:
        if key not in cols:
            continue
        added = await writer.ensure_named_column(route, tab, key, label)
        if added:
            print(f"  + header '{label}' at col {col_letter(cols[key])}")

    sid = writer._sid
    invs_on_sheet = find_invoices_in_range(
        writer._factory.sheets(), sid, tab_name, tab.tab_id,
        cols["INVOICE_NO"], cols["PO"], first_row, last_row,
    )
    print(f"  found {len(invs_on_sheet)} invoices on sheet in [{first_row}..{last_row}]")

    aligned, problems = _validate_alignment(invs_on_sheet, invoices_by_no, tab_name)
    if not aligned:
        print(f"  !! alignment mismatch - refusing to write:", file=sys.stderr)
        for p in problems:
            print(f"     {p}", file=sys.stderr)
        return False

    INVOICE_LEVEL_KEYS = [
        "TOTAL_INVOICE", "PIN_CODE", "E_WAY_BILL", "CUSTOMER_GSTIN",
        "PAYMENT_TERMS", "APPROX_DISTANCE", "IRN",
    ]
    ARTICLE_LEVEL_KEYS = ["HSN_CODE", "RATE_PER_UNIT"]
    merge_requests: list[dict] = []
    value_batches: list[dict] = []

    for io_obj in invs_on_sheet:
        inv = invoices_by_no.get(io_obj.invoice_no)
        if not inv:
            continue
        n_rows = io_obj.end_row - io_obj.start_row + 1
        for key in INVOICE_LEVEL_KEYS:
            if key not in cols:
                continue
            col = cols[key]
            value = _value_for_key(inv, key)
            if value in (None, ""):
                continue
            rng = f"'{tab_name}'!{col_letter(col)}{io_obj.start_row}:{col_letter(col)}{io_obj.end_row}"
            value_batches.append({"range": rng, "values": [[value] for _ in range(n_rows)]})
            if n_rows > 1:
                merge_requests.append({
                    "mergeCells": {
                        "range": {
                            "sheetId": tab.tab_id,
                            "startRowIndex": io_obj.start_row - 1,
                            "endRowIndex": io_obj.end_row,
                            "startColumnIndex": col - 1,
                            "endColumnIndex": col,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                })
        articles = inv.unique_articles
        if len(articles) == n_rows:
            for key in ARTICLE_LEVEL_KEYS:
                if key not in cols:
                    continue
                col = cols[key]
                rng = f"'{tab_name}'!{col_letter(col)}{io_obj.start_row}:{col_letter(col)}{io_obj.end_row}"
                if key == "HSN_CODE":
                    vals = [[a.hsn_code or ""] for a in articles]
                else:
                    vals = [[a.rate_per_unit if a.rate_per_unit is not None else ""] for a in articles]
                value_batches.append({"range": rng, "values": vals})
        print(f"  + {io_obj.invoice_no:<22} rows {io_obj.start_row}..{io_obj.end_row} ({n_rows} rows)")

    if writer.dry_run:
        print(f"  [dry] would write {len(value_batches)} value range(s) in 1 batch")
        print(f"  [dry] would apply {len(merge_requests)} merge request(s)")
    else:
        api = writer._factory.sheets()
        if value_batches:
            api.spreadsheets().values().batchUpdate(
                spreadsheetId=sid,
                body={"valueInputOption": "USER_ENTERED", "data": value_batches},
            ).execute()
            print(f"  wrote {len(value_batches)} value range(s) in 1 batch")
        end_col = max(cols.values())
        merge_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": tab.tab_id,
                    "startRowIndex": first_row - 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": 0,
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
        })
        api.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": merge_requests}
        ).execute()
        print(f"  applied {len(merge_requests)} merge+format requests")

    hide_cols = [cols[k] for k in HIDDEN_KEYS if k in cols]
    await writer.hide_columns(tab, hide_cols, label=tab_name)
    print(f"  hid cols: {[col_letter(c) for c in hide_cols]}")
    return True


def _value_for_key(inv: InvoiceData, key: str):
    if key == "TOTAL_INVOICE":
        return round(inv.total_gross, 2) if inv.total_gross else ""
    if key == "PIN_CODE":
        return inv.pin_code or ""
    if key == "E_WAY_BILL":
        return inv.e_way_bill_no or ""
    if key == "CUSTOMER_GSTIN":
        return inv.customer_gstin or ""
    if key == "PAYMENT_TERMS":
        return inv.payment_terms or ""
    if key == "APPROX_DISTANCE":
        return inv.approx_distance_km or ""
    if key == "IRN":
        return inv.irn or ""
    return None


async def main_async(zip_path: str, sid: str, live: bool) -> int:
    writer = build_writer(sid, dry_run=not live)
    invoices_by_no = parse_all(zip_path)
    print(f"parsed {len(invoices_by_no)} invoices from zip")
    print(f"mode: {'LIVE WRITE' if live else 'DRY RUN'}")

    any_failed = False
    for route, tab_name, first, last in MAY_9_2026_ROW_RANGES:
        ok = await patch_tab(writer, route, tab_name, first, last, invoices_by_no)
        if not ok:
            any_failed = True
    return 1 if any_failed else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path", help="Path to zip of the 09-May batch invoices")
    ap.add_argument("sheet", help="Sheet URL or ID")
    ap.add_argument("--live", action="store_true",
                    help="Actually write to the sheet (default: dry-run)")
    args = ap.parse_args()
    sid = extract_spreadsheet_id(args.sheet)
    sys.exit(asyncio.run(main_async(args.zip_path, sid, live=args.live)))


if __name__ == "__main__":
    main()
