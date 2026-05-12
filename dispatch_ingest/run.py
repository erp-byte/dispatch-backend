"""End-to-end ingest: PDFs in a zip → Google Sheet (with merges).

Usage:
    python -m dispatch_ingest.run <zip_path> <sheet_url_or_id> [--live] [--strict]

Without --live, runs in dry mode (no writes). Both modes print the full
preview so you can sanity-check before committing the data.

With --strict, parse failures cause a non-zero exit code (suitable for CI).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import zipfile

from dispatch_ingest.classifier import classify, Route
from dispatch_ingest.parser import parse_pdf
from dispatch_ingest.rows import (
    SCHEMA_BY_ROUTE, NEW_COLUMN_HEADERS, HIDDEN_KEYS, InvoiceRowBlock, build_rows,
)
from dispatch_ingest.utils import col_letter, ensure_utf8_stdout, inv_no_key
from dispatch_ingest.writer import build_writer, extract_spreadsheet_id


ensure_utf8_stdout()


_TAB_BY_ROUTE = {
    Route.CFPL: "CFPL",
    Route.CDPL: "CDPL",
    Route.SERVICE_CFPL: "SERVICE CFPL",
}


def collect_blocks(zip_path: str) -> tuple[dict[Route, list[InvoiceRowBlock]], list[tuple[str, str]]]:
    """Parse all PDFs in `zip_path`. Returns (blocks_by_route, parse_errors)."""
    blocks_by_route: dict[Route, list[InvoiceRowBlock]] = {
        Route.CFPL: [], Route.CDPL: [], Route.SERVICE_CFPL: [],
    }
    skipped: list[tuple[str, str]] = []
    parse_errors: list[tuple[str, str]] = []

    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zip_path) as z:
            for name in sorted(z.namelist()):
                if not name.lower().endswith(".pdf"):
                    continue
                cls = classify(name)
                if cls.route == Route.SKIP:
                    skipped.append((name, cls.reason))
                    continue
                local = os.path.join(td, os.path.basename(name))
                with z.open(name) as src, open(local, "wb") as dst:
                    dst.write(src.read())
                try:
                    inv = parse_pdf(local)
                except Exception as e:  # noqa: BLE001  parse failures are recoverable
                    parse_errors.append((name, str(e)))
                    continue
                blocks_by_route[cls.route].append(build_rows(inv, cls.route))

    for r in blocks_by_route:
        blocks_by_route[r].sort(key=lambda b: inv_no_key(b.invoice_no))

    print("\n--- Parse summary ---")
    for r, blocks in blocks_by_route.items():
        tab_name, _, _ = SCHEMA_BY_ROUTE[r]
        total_rows = sum(b.n_articles for b in blocks)
        print(f"  {tab_name:<14} {len(blocks):>3} invoice(s)  ->  {total_rows:>3} new row(s)")
    print(f"  SKIPPED       {len(skipped):>3} file(s)")
    if parse_errors:
        print(f"  PARSE ERRORS  {len(parse_errors):>3}", file=sys.stderr)
        for f, e in parse_errors:
            print(f"    ! {f}: {e}", file=sys.stderr)

    return blocks_by_route, parse_errors


async def run_ingest(zip_path: str, spreadsheet_id: str, live: bool, strict: bool = False) -> int:
    """Returns process exit code (0 = success)."""
    blocks_by_route, parse_errors = collect_blocks(zip_path)

    writer = build_writer(spreadsheet_id, dry_run=not live)
    sa_email = getattr(writer._factory.credentials, "service_account_email", "(unknown)")
    print(f"\n  authenticated as: {sa_email}")
    print(f"  spreadsheet id  : {spreadsheet_id}")
    print(f"  mode            : {'LIVE WRITE' if live else 'DRY RUN (no writes)'}")

    tabs = await writer.list_tabs()
    print(f"  tabs available  : {sorted(tabs.keys())}")

    for route, blocks in blocks_by_route.items():
        tab_name = _TAB_BY_ROUTE[route]
        if tab_name not in tabs:
            print(f"  !! tab '{tab_name}' not found in spreadsheet - skipping")
            continue
        tab = tabs[tab_name]
        print(f"\n=== {tab_name}  (tab_id={tab.tab_id}, grid {tab.grid_rows}rx{tab.grid_cols}c) ===")

        cols_map = SCHEMA_BY_ROUTE[route][1]
        for key, label in NEW_COLUMN_HEADERS:
            if key not in cols_map:
                continue
            added = await writer.ensure_named_column(route, tab, key, label)
            letter = col_letter(cols_map[key])
            marker = "+ added" if added else "= present:"
            print(f"  {marker} '{label}' header at col {letter}")
        hide_cols = [cols_map[k] for k in HIDDEN_KEYS if k in cols_map]
        await writer.hide_columns(tab, hide_cols, label=f"{tab_name} hidden cols")

        if not blocks:
            print("  no new invoices for this tab - schema setup done, nothing to write")
            continue

        existing = await writer.fetch_existing_invoice_nos(route, tab)
        print(f"  existing invoice count on tab: {len(existing)}")

        new_blocks = [b for b in blocks if b.invoice_no not in existing]
        skipped_dup = len(blocks) - len(new_blocks)
        for dup in (b for b in blocks if b.invoice_no in existing):
            print(f"  skip duplicate: {dup.invoice_no}")

        po_col = cols_map["PO"]
        clusters = po_clusters(new_blocks)
        in_cluster: set[str] = {b.invoice_no for cluster, _ in clusters for b in cluster}

        block_row_ranges: dict[str, tuple[int, int]] = {}
        for block in new_blocks:
            skip = {po_col} if block.invoice_no in in_cluster else set()
            start_row, end_row = await writer.write_block(block, tab, skip_merge_cols=skip)
            block_row_ranges[block.invoice_no] = (start_row, end_row)
            print(f"  + {block.invoice_no:<20}  rows {start_row}..{end_row}  "
                  f"({block.n_articles} articles, {block.customer[:30]})")
        wrote = sum(b.n_articles for b in new_blocks)

        for cluster, shared_po in clusters:
            first = block_row_ranges[cluster[0].invoice_no][0]
            last = block_row_ranges[cluster[-1].invoice_no][1]
            label = (f"PO {shared_po} across {len(cluster)} invoices "
                     f"({cluster[0].invoice_no}..{cluster[-1].invoice_no})")
            await writer.merge_range(tab, po_col, first, last, label=label)

        if clusters:
            print(f"  -- cross-invoice PO merges: {len(clusters)} cluster(s)")
        print(f"  -- summary: wrote {wrote} row(s), skipped {skipped_dup} duplicate invoice(s)")

    if strict and parse_errors:
        print(f"\nFAILED: {len(parse_errors)} parse error(s) in --strict mode", file=sys.stderr)
        return 2
    return 0


def po_clusters(blocks: list[InvoiceRowBlock]) -> list[tuple[list[InvoiceRowBlock], str]]:
    """Group consecutive blocks that share the same non-blank PO."""
    def po_of(b: InvoiceRowBlock) -> str:
        idx = SCHEMA_BY_ROUTE[b.route][1]["PO"] - 1
        if not b.rows or idx >= len(b.rows[0]):
            return ""
        return str(b.rows[0][idx] or "")

    clusters: list[tuple[list[InvoiceRowBlock], str]] = []
    i = 0
    while i < len(blocks):
        po = po_of(blocks[i])
        if not po:
            i += 1
            continue
        j = i + 1
        while j < len(blocks) and po_of(blocks[j]) == po:
            j += 1
        if j - i >= 2:
            clusters.append((blocks[i:j], po))
        i = j
    return clusters


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path", help="Path to invoice zip (e.g. docs/090526.zip)")
    ap.add_argument("sheet", help="Google Sheet URL or ID")
    ap.add_argument("--live", action="store_true",
                    help="Actually write to the sheet (default is dry-run)")
    ap.add_argument("--strict", action="store_true",
                    help="Exit non-zero if any PDF fails to parse")
    args = ap.parse_args()
    sid = extract_spreadsheet_id(args.sheet)
    sys.exit(asyncio.run(run_ingest(args.zip_path, sid, live=args.live, strict=args.strict)))


if __name__ == "__main__":
    main()
