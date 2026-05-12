"""Dry-run the parser + classifier against PDFs in a zip and print a preview."""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from typing import Optional

from dispatch_ingest.classifier import classify, Route
from dispatch_ingest.parser import parse_pdf, InvoiceData
from dispatch_ingest.rows import build_rows, SCHEMA_BY_ROUTE
from dispatch_ingest.utils import col_letter, ensure_utf8_stdout, inv_no_key


ensure_utf8_stdout()


def main(zip_path: str) -> None:
    by_route: dict[Route, list[tuple[str, Optional[InvoiceData]]]] = {
        Route.CFPL: [], Route.CDPL: [], Route.SERVICE_CFPL: [], Route.SKIP: [],
    }

    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zip_path) as z:
            for name in sorted(z.namelist()):
                if not name.lower().endswith(".pdf"):
                    continue
                cls = classify(name)
                if cls.route == Route.SKIP:
                    by_route[Route.SKIP].append((name, None))
                    continue
                local = os.path.join(td, os.path.basename(name))
                with z.open(name) as src, open(local, "wb") as dst:
                    dst.write(src.read())
                try:
                    inv = parse_pdf(local)
                except Exception as e:  # noqa: BLE001
                    print(f"[ERR ] {name}: parse failed - {e}", file=sys.stderr)
                    by_route[Route.SKIP].append((name, None))
                    continue
                by_route[cls.route].append((name, inv))

    for r in (Route.CFPL, Route.CDPL, Route.SERVICE_CFPL):
        by_route[r].sort(key=lambda pair: inv_no_key(pair[1].invoice_no if pair[1] else ""))

    for route in (Route.CFPL, Route.CDPL, Route.SERVICE_CFPL):
        items = by_route[route]
        tab_name, cols, width = SCHEMA_BY_ROUTE[route]
        print(f"\n{'=' * 100}")
        print(f"  {tab_name}  -  {len(items)} invoice(s)   |   article col = {col_letter(cols['ARTICLE'])}, row width = {width}")
        print('=' * 100)
        total_rows = 0
        for fn, inv in items:
            if not inv:
                print(f"  ! parse failed for {fn}")
                continue
            blk = build_rows(inv, route)
            total_rows += blk.n_articles
            print(f"\n* {inv.invoice_no}  ({inv.customer})  ->  {blk.n_articles} row(s); merge cols {[col_letter(c) for c in blk.merge_cols]}")
            print(f"    date {inv.date_iso}  | loc {inv.location}  | po {inv.po_no}  | "
                  f"transporter {inv.transporter}  | vehicle {inv.lr_no}  | freight Rs.{inv.freight_charge:,.2f}")
            for r in blk.rows:
                art = r[cols["ARTICLE"] - 1]
                b = r[cols["BOXES"] - 1]
                w = r[cols["NET_WT"] - 1]
                a = r[cols["WITHOUT_TAX"] - 1] if "WITHOUT_TAX" in cols else 0.0
                print(f"      [{art[:55]:<55}]  boxes={b:>4}  net_kg={w:>9}  amount=Rs.{a:>12,.2f}")
        print(f"\n  -> would write {total_rows} new row(s) to '{tab_name}'")

    skipped = by_route[Route.SKIP]
    print(f"\n{'=' * 100}\n  SKIPPED  -  {len(skipped)} file(s)\n{'=' * 100}")
    for fn, _ in skipped:
        cls = classify(fn)
        print(f"  - {fn}  ({cls.reason})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/090526.zip")
