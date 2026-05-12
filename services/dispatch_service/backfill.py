"""Reverse-mapper: reconstruct a ParsedInvoice from sheet row arrays.

Mirrors dispatch_ingest.rows.build_rows in reverse. Used only by the backfill
flow — the live ingest path uses parse_pdf → InvoiceData → build_rows directly.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Optional

from dispatch_ingest.classifier import Route
from dispatch_ingest.rows import SCHEMA_BY_ROUTE
from services.dispatch_service.models import ParsedArticle, ParsedInvoice


_SHEETS_EPOCH = date(1899, 12, 30)
_DATE_DDMMM_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{2,4})$")
_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_int_or_none(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None


def _parse_decimal_or_none(v: Any) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_sheet_date(v: Any) -> Optional[date]:
    """Parse any of: ISO 'YYYY-MM-DD', Tally 'DD-Mon-YY', or Sheets serial int."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            return _SHEETS_EPOCH + timedelta(days=int(v))
        except (OverflowError, TypeError):
            return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    m = _DATE_DDMMM_RE.match(s)
    if m:
        d, mon, y = m.group(1), m.group(2).capitalize(), m.group(3)
        mm = _MONTHS.get(mon)
        if mm is None:
            return None
        yy = int(y)
        if yy < 100:
            yy += 2000
        try:
            return date(yy, mm, int(d))
        except ValueError:
            return None
    return None


def _to_float(v: Any) -> float:
    """Best-effort float — empty becomes 0."""
    d = _parse_decimal_or_none(v)
    return float(d) if d is not None else 0.0


def _row_block_to_invoice(rows: list[list[Any]], route: Route) -> ParsedInvoice:
    """Reconstruct a ParsedInvoice from N sheet rows for one invoice.

    Invoice-level fields are read from rows[0] (other rows in a merged-cell
    block share the same value). Per-article fields come from each row.
    """
    _name, cols, _width = SCHEMA_BY_ROUTE[route]
    first = rows[0]

    def cell(row: list[Any], key: str) -> Any:
        if key not in cols:
            return ""
        return row[cols[key] - 1]

    inv_date = _parse_sheet_date(cell(first, "DATE"))
    date_iso = inv_date.isoformat() if inv_date else ""

    total_taxable = 0.0
    if "WITHOUT_TAX" in cols:
        total_taxable = sum(_to_float(cell(r, "WITHOUT_TAX")) for r in rows)
    total_gross = _to_float(cell(first, "TOTAL_INVOICE")) if "TOTAL_INVOICE" in cols else total_taxable
    freight = _to_float(cell(first, "TRANSPORT_CHARGES"))

    articles: list[ParsedArticle] = []
    for r in rows:
        article = _str_or_none(cell(r, "ARTICLE"))
        if article is None:
            continue
        rate_dec = _parse_decimal_or_none(cell(r, "RATE_PER_UNIT"))
        articles.append(ParsedArticle(
            article=article,
            boxes=_parse_int_or_none(cell(r, "BOXES")) or 0,
            net_wt_kg=_to_float(cell(r, "NET_WT")),
            taxable_amount=_to_float(cell(r, "WITHOUT_TAX")) if "WITHOUT_TAX" in cols else 0.0,
            hsn_code=_str_or_none(cell(r, "HSN_CODE")),
            rate_per_unit=float(rate_dec) if rate_dec is not None else None,
        ))

    return ParsedInvoice(
        invoice_no=str(cell(first, "INVOICE_NO") or ""),
        date_iso=date_iso,
        customer=str(cell(first, "CUSTOMER") or ""),
        location=str(cell(first, "LOCATION") or ""),
        po_no=_str_or_none(cell(first, "PO")),
        transporter=_str_or_none(cell(first, "TRANSPORTER")),
        lr_no=_str_or_none(cell(first, "LR_NO")),
        invoice_type="tax",
        total_taxable=total_taxable,
        total_gross=total_gross,
        freight_charge=freight,
        warehouse=_str_or_none(cell(first, "WAREHOUSE")),
        pin_code=_str_or_none(cell(first, "PIN_CODE")) if "PIN_CODE" in cols else None,
        e_way_bill_no=_str_or_none(cell(first, "E_WAY_BILL")),
        customer_gstin=_str_or_none(cell(first, "CUSTOMER_GSTIN")),
        payment_terms=_str_or_none(cell(first, "PAYMENT_TERMS")),
        approx_distance_km=_parse_int_or_none(cell(first, "APPROX_DISTANCE")),
        irn=_str_or_none(cell(first, "IRN")),
        articles=articles,
    )
