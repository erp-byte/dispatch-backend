"""DispatchRepository — async CRUD on billed_details + billed_articles.

Soft-delete only — no hard DELETE statement appears in this module. All reads
go through the views `v_billed_details` / `v_billed_articles` so callers never
see soft-deleted rows.
"""

from __future__ import annotations

from datetime import date as _date
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from services.dispatch_service.ids import new_article_ids, new_invoice_id
from services.dispatch_service.models import ParsedInvoice
from services.dispatch_service.sku_matcher import SkuMatcher


_UPSERT_DETAILS_SQL = """
INSERT INTO billed_details (
    id, route, invoice_no, invoice_date, invoice_type, customer, location,
    pin_code, customer_gstin, po_no, transporter, lr_no, warehouse,
    e_way_bill_no, payment_terms, approx_distance_km, irn,
    total_taxable, total_gross, freight_charge, transport_per_kg, dispatch_unit,
    source_pdf, sheet_start_row, sheet_end_row
) VALUES (
    $1, $2, $3, $4, $5, $6, $7,
    $8, $9, $10, $11, $12, $13,
    $14, $15, $16, $17,
    $18, $19, $20, $21, $22,
    $23, $24, $25
)
ON CONFLICT ON CONSTRAINT billed_details_route_invoice_uk DO NOTHING
RETURNING id
"""

_LOOKUP_EXISTING_DETAILS_SQL = """
SELECT id FROM billed_details
WHERE route = $1 AND invoice_no = $2 AND deleted_at IS NULL
"""

_INSERT_ARTICLE_SQL = """
INSERT INTO billed_articles (
    id, billed_details_id, article_index, article, boxes,
    net_wt_kg, gr_wt_kg, taxable_amount, hsn_code, rate_per_unit,
    matched_sku_id, matched_item_type, matched_item_group, matched_sub_group,
    matched_uom, matched_gst, matched_sale_group
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14, $15, $16, $17
)
ON CONFLICT ON CONSTRAINT billed_articles_invoice_slot_uk DO NOTHING
"""


# Map sheet field key → DB column on billed_details.
_INVOICE_FIELDS: dict[str, str] = {
    "DATE": "invoice_date",
    "CUSTOMER": "customer",
    "LOCATION": "location",
    "PO": "po_no",
    "TRANSPORTER": "transporter",
    "LR_NO": "lr_no",
    "TRANSPORT_CHARGES": "freight_charge",
    "TOTAL_INVOICE": "total_gross",
    "PIN_CODE": "pin_code",
    "TRANSPORT_PER_KG": "transport_per_kg",
    "WAREHOUSE": "warehouse",
    "E_WAY_BILL": "e_way_bill_no",
    "CUSTOMER_GSTIN": "customer_gstin",
    "PAYMENT_TERMS": "payment_terms",
    "APPROX_DISTANCE": "approx_distance_km",
    "IRN": "irn",
    "DISPATCH_UNIT": "dispatch_unit",
}

# Map sheet field key → DB column on billed_articles.
_ARTICLE_FIELDS: dict[str, str] = {
    "BOXES": "boxes",
    "NET_WT": "net_wt_kg",
    "GR_WT": "gr_wt_kg",
    "ARTICLE": "article",
    "HSN_CODE": "hsn_code",
    "RATE_PER_UNIT": "rate_per_unit",
    "WITHOUT_TAX": "taxable_amount",
}


def _parse_rowcount(status: str) -> int:
    """asyncpg returns 'UPDATE N' from conn.execute() — extract N."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


def _transport_per_kg(invoice: ParsedInvoice) -> Optional[Decimal]:
    total_net = sum(Decimal(str(a.net_wt_kg)) for a in invoice.articles)
    if not invoice.freight_charge or not total_net:
        return None
    return (Decimal(str(invoice.freight_charge)) / total_net).quantize(Decimal("0.0001"))


class DispatchRepository:
    """All DB operations for the dispatch service. Soft-delete only.

    Construct with a connected asyncpg pool. Each method acquires its own
    connection from the pool.
    """

    def __init__(self, pool: asyncpg.Pool,
                 sku_matcher: Optional[SkuMatcher] = None):
        self._pool = pool
        self._sku_matcher = sku_matcher

    async def upsert_invoice(
        self, *,
        invoice: ParsedInvoice,
        route: str,
        sheet_start_row: Optional[int],
        sheet_end_row: Optional[int],
        source_pdf: Optional[str],
    ) -> str:
        """Insert an invoice and its articles in one transaction.

        Returns the billed_details.id (either the newly-created one or the
        existing id if (route, invoice_no) already had an active row).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                invoice_id = await new_invoice_id(conn)
                inserted = await conn.fetchrow(
                    _UPSERT_DETAILS_SQL,
                    invoice_id, route, invoice.invoice_no,
                    _date.fromisoformat(invoice.date_iso),
                    invoice.invoice_type, invoice.customer, invoice.location,
                    invoice.pin_code, invoice.customer_gstin, invoice.po_no,
                    invoice.transporter, invoice.lr_no, invoice.warehouse,
                    invoice.e_way_bill_no, invoice.payment_terms,
                    invoice.approx_distance_km, invoice.irn,
                    Decimal(str(invoice.total_taxable)),
                    Decimal(str(invoice.total_gross)),
                    Decimal(str(invoice.freight_charge or 0)),
                    _transport_per_kg(invoice),
                    None,  # dispatch_unit — sheet-side manual fill
                    source_pdf, sheet_start_row, sheet_end_row,
                )
                if inserted is None:
                    existing = await conn.fetchrow(
                        _LOOKUP_EXISTING_DETAILS_SQL, route, invoice.invoice_no,
                    )
                    return existing["id"] if existing else invoice_id

                created_id = inserted["id"]
                if invoice.articles:
                    art_ids = await new_article_ids(conn, len(invoice.articles))
                    params = []
                    for i, (a, art_id) in enumerate(zip(invoice.articles, art_ids)):
                        match = (self._sku_matcher.match(a.article)
                                 if self._sku_matcher is not None else None)
                        params.append((
                            art_id, created_id, i,
                            a.article,
                            int(a.boxes or 0),
                            Decimal(str(a.net_wt_kg or 0)),
                            None,  # gr_wt_kg
                            Decimal(str(a.taxable_amount or 0)),
                            a.hsn_code,
                            Decimal(str(a.rate_per_unit)) if a.rate_per_unit is not None else None,
                            (match or {}).get("matched_sku_id"),
                            (match or {}).get("matched_item_type"),
                            (match or {}).get("matched_item_group"),
                            (match or {}).get("matched_sub_group"),
                            (match or {}).get("matched_uom"),
                            (match or {}).get("matched_gst"),
                            (match or {}).get("matched_sale_group"),
                        ))
                    await conn.executemany(_INSERT_ARTICLE_SQL, params)
                return created_id

    async def update_field(
        self, *,
        route: str, invoice_no: str, field: str, value: Any,
        article_index: Optional[int],
    ) -> int:
        """Update one field on an invoice (or one article row).

        Returns the number of DB rows affected. Raises ValueError if the field
        name is not recognised.
        """
        if field in _INVOICE_FIELDS:
            col = _INVOICE_FIELDS[field]
            async with self._pool.acquire() as conn:
                status = await conn.execute(
                    f"UPDATE billed_details SET {col} = $1 "
                    f"WHERE route = $2 AND invoice_no = $3 AND deleted_at IS NULL",
                    value, route, invoice_no,
                )
            return _parse_rowcount(status)

        if field in _ARTICLE_FIELDS:
            col = _ARTICLE_FIELDS[field]
            async with self._pool.acquire() as conn:
                if article_index is None:
                    status = await conn.execute(
                        f"UPDATE billed_articles SET {col} = $1 "
                        f"WHERE billed_details_id IN ("
                        f"  SELECT id FROM billed_details "
                        f"  WHERE route = $2 AND invoice_no = $3 AND deleted_at IS NULL"
                        f") AND deleted_at IS NULL",
                        value, route, invoice_no,
                    )
                else:
                    status = await conn.execute(
                        f"UPDATE billed_articles SET {col} = $1 "
                        f"WHERE billed_details_id IN ("
                        f"  SELECT id FROM billed_details "
                        f"  WHERE route = $2 AND invoice_no = $3 AND deleted_at IS NULL"
                        f") AND article_index = $4 AND deleted_at IS NULL",
                        value, route, invoice_no, article_index,
                    )
            return _parse_rowcount(status)

        raise ValueError(f"unknown field {field!r}")

    async def update_po_for_invoices(
        self, *, route: str, invoice_nos: list[str], po_no: str,
    ) -> int:
        """Set po_no = $po on every billed_details row whose invoice_no is in the list."""
        if not invoice_nos:
            return 0
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE billed_details SET po_no = $1 "
                "WHERE route = $2 AND invoice_no = ANY($3) AND deleted_at IS NULL",
                po_no, route, invoice_nos,
            )
        return _parse_rowcount(status)

    async def soft_delete_invoice(
        self, *, route: str, invoice_no: str,
    ) -> Optional[dict[str, Any]]:
        """Mark an invoice and all its articles as deleted_at = now().

        Returns {'billed_details_id': <id>, 'articles_deleted': <n>} on success,
        or None if no matching active row existed.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                detail = await conn.fetchrow(
                    "UPDATE billed_details SET deleted_at = now() "
                    "WHERE route = $1 AND invoice_no = $2 AND deleted_at IS NULL "
                    "RETURNING id",
                    route, invoice_no,
                )
                if detail is None:
                    return None
                status = await conn.execute(
                    "UPDATE billed_articles SET deleted_at = now() "
                    "WHERE billed_details_id = $1 AND deleted_at IS NULL",
                    detail["id"],
                )
                return {
                    "billed_details_id": detail["id"],
                    "articles_deleted": _parse_rowcount(status),
                }

    async def check_existing(
        self, *, route: str, invoice_nos: list[str],
    ) -> set[str]:
        """Return the subset of invoice_nos that already exist (active) in the DB."""
        if not invoice_nos:
            return set()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT invoice_no FROM billed_details "
                "WHERE route = $1 AND invoice_no = ANY($2) AND deleted_at IS NULL",
                route, invoice_nos,
            )
        return {r["invoice_no"] for r in rows}
