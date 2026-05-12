"""Turn parsed InvoiceData into per-article rows for the target sheet tab.

Schema reminder (after Article column is appended at the right edge):

- CFPL / CDPL row (cols A..R):
    A DATE, B CUSTOMER, C LOCATION, D PO. NO., E Invoice No,
    F Boxes, G NET.WT, H GR. WT., I TRANSPORTER, J LR NO., K Transport Charges,
    L (blank), M (blank), N Without Tax value, O Total Invoice, P Pin Code,
    Q Transport Charges Per Kg, R Article  <-- new column

- SERVICE CFPL row (cols A..N):
    A DATE, B CUSTOMER, C LOCATION, D PO. NO., E INV NO.,
    F Boxes, G NET.WT, H GR. WT., I TRANSPORTER, J LR NO., K Transport Charges,
    L (blank), M DISPATCH UNIT, N Article  <-- new column

For each invoice with N unique articles we emit N rows; cols other than
Article + per-line Boxes/NET.WT/Without Tax value carry the invoice-level value
on every row (the writer then merges those cells visually).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dispatch_ingest.classifier import Route
from dispatch_ingest.parser import InvoiceData, LineItem


# 1-indexed column numbers for clarity.
# Existing CFPL/CDPL cols A..Q (1..17) preserved; new fields start at R.
# Invoice-level new cols (merged): T..Y. Article-level (per-row, not merged): Z..AA.
CFPL_COLS = {
    "DATE": 1, "CUSTOMER": 2, "LOCATION": 3, "PO": 4, "INVOICE_NO": 5,
    "BOXES": 6, "NET_WT": 7, "GR_WT": 8, "TRANSPORTER": 9, "LR_NO": 10,
    "TRANSPORT_CHARGES": 11, "WITHOUT_TAX": 14, "TOTAL_INVOICE": 15,
    "PIN_CODE": 16, "TRANSPORT_PER_KG": 17,
    "ARTICLE": 18, "WAREHOUSE": 19,
    "E_WAY_BILL": 20, "CUSTOMER_GSTIN": 21, "PAYMENT_TERMS": 22,
    "APPROX_DISTANCE": 23, "IRN": 24,
    "HSN_CODE": 25, "RATE_PER_UNIT": 26,
}
CDPL_COLS = dict(CFPL_COLS)
# SERVICE CFPL keeps its own narrower layout (no Without Tax / Total Invoice / Pin Code / per-kg).
SERVICE_COLS = {
    "DATE": 1, "CUSTOMER": 2, "LOCATION": 3, "PO": 4, "INVOICE_NO": 5,
    "BOXES": 6, "NET_WT": 7, "GR_WT": 8, "TRANSPORTER": 9, "LR_NO": 10,
    "TRANSPORT_CHARGES": 11, "DISPATCH_UNIT": 13,
    "ARTICLE": 14, "WAREHOUSE": 15,
    "E_WAY_BILL": 16, "CUSTOMER_GSTIN": 17, "PAYMENT_TERMS": 18,
    "APPROX_DISTANCE": 19, "IRN": 20,
    "HSN_CODE": 21, "RATE_PER_UNIT": 22,
}


SCHEMA_BY_ROUTE = {
    Route.CFPL: ("CFPL", CFPL_COLS, 26),
    Route.CDPL: ("CDPL", CDPL_COLS, 26),
    Route.SERVICE_CFPL: ("SERVICE CFPL", SERVICE_COLS, 22),
}

# I12: schema-drift guard. If anyone adds a new key past the configured `width`,
# `write_block` would silently drop the value because it sizes the row to width.
# This assertion runs at import time so the regression surfaces immediately.
for _route, (_name, _cols, _width) in SCHEMA_BY_ROUTE.items():
    assert max(_cols.values()) == _width, (
        f"schema-width mismatch for {_route.value}: max(cols)={max(_cols.values())}"
        f" but configured width={_width}. Update SCHEMA_BY_ROUTE."
    )
del _route, _name, _cols, _width

# Header label for each new key. Existing cols (DATE, CUSTOMER, …) are left
# alone — only the new ones get a header injected.
NEW_COLUMN_HEADERS = [
    ("TOTAL_INVOICE", "Total Invoice"),
    ("PIN_CODE", "Pin Code"),
    ("TRANSPORT_PER_KG", "Transport Charges Per Kg"),
    ("ARTICLE", "Article"),
    ("WAREHOUSE", "Warehouse"),
    ("E_WAY_BILL", "e-Way Bill No"),
    ("CUSTOMER_GSTIN", "Customer GSTIN"),
    ("PAYMENT_TERMS", "Payment Terms"),
    ("APPROX_DISTANCE", "Approx Distance (km)"),
    ("IRN", "IRN"),
    ("HSN_CODE", "HSN"),
    ("RATE_PER_UNIT", "Rate"),
]

# Columns to hide by default on each tab (the user wants Article + new cols hidden)
HIDDEN_KEYS = ("ARTICLE", "E_WAY_BILL", "CUSTOMER_GSTIN", "PAYMENT_TERMS",
               "APPROX_DISTANCE", "IRN", "HSN_CODE", "RATE_PER_UNIT")


@dataclass
class InvoiceRowBlock:
    """One invoice expanded into N article rows + merge instructions."""
    route: Route
    invoice_no: str
    n_articles: int
    rows: list[list[Any]]  # row-major; each row is the full row width (cols 1..max)
    merge_cols: list[int]  # 1-indexed columns to merge vertically across the N rows
    customer: str  # for logging


def build_rows(invoice: InvoiceData, route: Route) -> InvoiceRowBlock:
    """Convert a parsed invoice into a block of per-article rows for one tab."""
    if route not in SCHEMA_BY_ROUTE:
        raise ValueError(f"no schema for route {route}")
    _tab_name, cols, width = SCHEMA_BY_ROUTE[route]

    articles = invoice.unique_articles
    if not articles:
        # Service bills with no parseable line item — emit one zero-row so the
        # invoice still appears (with manual fill flag)
        articles = [LineItem(article="(no article extracted)", boxes=0,
                             net_wt_kg=0.0, taxable_amount=invoice.total_taxable)]

    # I11: transport-per-kg is an INVOICE-LEVEL ratio (freight / total net wt).
    # Same value for every article row, so it gets merged across the block.
    total_net_kg = sum(a.net_wt_kg for a in articles)
    transport_per_kg = (
        round(invoice.freight_charge / total_net_kg, 2)
        if invoice.freight_charge and total_net_kg
        else ""
    )

    rows: list[list[Any]] = []
    for li in articles:
        row: list[Any] = [""] * width
        row[cols["DATE"] - 1] = invoice.date_iso
        row[cols["CUSTOMER"] - 1] = invoice.customer
        row[cols["LOCATION"] - 1] = invoice.location
        row[cols["PO"] - 1] = invoice.po_no or ""
        row[cols["INVOICE_NO"] - 1] = invoice.invoice_no
        row[cols["BOXES"] - 1] = li.boxes
        row[cols["NET_WT"] - 1] = round(li.net_wt_kg, 3)
        row[cols["GR_WT"] - 1] = ""
        row[cols["TRANSPORTER"] - 1] = invoice.transporter or ""
        row[cols["LR_NO"] - 1] = invoice.lr_no or ""
        row[cols["TRANSPORT_CHARGES"] - 1] = invoice.freight_charge or 0
        if "WITHOUT_TAX" in cols:
            row[cols["WITHOUT_TAX"] - 1] = round(li.taxable_amount, 2)
        if "TOTAL_INVOICE" in cols:
            row[cols["TOTAL_INVOICE"] - 1] = round(invoice.total_gross or 0, 2)
        if "PIN_CODE" in cols:
            row[cols["PIN_CODE"] - 1] = invoice.pin_code or ""
        if "TRANSPORT_PER_KG" in cols:
            row[cols["TRANSPORT_PER_KG"] - 1] = transport_per_kg
        row[cols["ARTICLE"] - 1] = li.article
        row[cols["WAREHOUSE"] - 1] = invoice.warehouse or ""
        row[cols["E_WAY_BILL"] - 1] = invoice.e_way_bill_no or ""
        row[cols["CUSTOMER_GSTIN"] - 1] = invoice.customer_gstin or ""
        row[cols["PAYMENT_TERMS"] - 1] = invoice.payment_terms or ""
        row[cols["APPROX_DISTANCE"] - 1] = invoice.approx_distance_km or ""
        row[cols["IRN"] - 1] = invoice.irn or ""
        row[cols["HSN_CODE"] - 1] = li.hsn_code or ""
        row[cols["RATE_PER_UNIT"] - 1] = (li.rate_per_unit
                                          if li.rate_per_unit is not None else "")
        rows.append(row)

    # Per-row (NOT merged): truly article-varying values only.
    # TRANSPORT_PER_KG is now invoice-level (I11) — it goes back into merge_cols.
    per_row_keys = {"BOXES", "NET_WT", "WITHOUT_TAX", "ARTICLE", "HSN_CODE", "RATE_PER_UNIT"}
    merge_cols = sorted(c for k, c in cols.items() if k not in per_row_keys)

    return InvoiceRowBlock(
        route=route,
        invoice_no=invoice.invoice_no,
        n_articles=len(articles),
        rows=rows,
        merge_cols=merge_cols,
        customer=invoice.customer,
    )
