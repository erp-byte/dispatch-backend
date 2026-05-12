"""Pydantic models mirroring DB row shape.

These are distinct from the existing `ParsedInvoice` / `ParsedArticle` types
in models.py — those are sheet-write request types, these are DB row types.
Kept separate so the wire schema and the DB schema can evolve independently.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class BilledDetailsRow(BaseModel):
    """One row of `billed_details` (or `v_billed_details` for reads)."""
    id: str
    route: str
    invoice_no: str
    invoice_date: date
    invoice_type: str
    customer: str
    location: str
    pin_code: Optional[str] = None
    customer_gstin: Optional[str] = None
    po_no: Optional[str] = None
    transporter: Optional[str] = None
    lr_no: Optional[str] = None
    warehouse: Optional[str] = None
    e_way_bill_no: Optional[str] = None
    payment_terms: Optional[str] = None
    approx_distance_km: Optional[int] = None
    irn: Optional[str] = None
    total_taxable: Decimal
    total_gross: Decimal
    freight_charge: Decimal = Field(default=Decimal("0"))
    transport_per_kg: Optional[Decimal] = None
    dispatch_unit: Optional[str] = None
    source_pdf: Optional[str] = None
    sheet_start_row: Optional[int] = None
    sheet_end_row: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None


class BilledArticleRow(BaseModel):
    """One row of `billed_articles` (or `v_billed_articles` for reads)."""
    id: str
    billed_details_id: str
    article_index: int
    article: str
    boxes: int = 0
    net_wt_kg: Decimal = Field(default=Decimal("0"))
    gr_wt_kg: Optional[Decimal] = None
    taxable_amount: Decimal = Field(default=Decimal("0"))
    hsn_code: Optional[str] = None
    rate_per_unit: Optional[Decimal] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
