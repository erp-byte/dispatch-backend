"""Pydantic request/response models for the dispatch MCP tools."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# Three routes the sheet supports; aligned with dispatch_ingest.classifier.Route
RouteName = Literal["CFPL", "CDPL", "SERVICE CFPL"]


# -----------------------------------------------------------------------------
# Common pieces
# -----------------------------------------------------------------------------

class _WithSheet(BaseModel):
    """Optional spreadsheet_id — falls back to SPREADSHEET_ID from .env."""
    spreadsheet_id: Optional[str] = Field(
        default=None,
        description="Sheet URL or ID. If omitted, uses SPREADSHEET_ID from server env.",
    )


# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------

class SetupSchemaRequest(_WithSheet):
    """Ensure all new column headers exist + hidden columns are hidden on a tab."""
    tab: RouteName


class SetupSchemaResponse(BaseModel):
    tab: str
    headers_added: list[str] = Field(default_factory=list)
    headers_already_present: list[str] = Field(default_factory=list)
    columns_hidden: list[str] = Field(default_factory=list,
        description="Column letters that are now hidden (newly or already).")


# -----------------------------------------------------------------------------
# Create — parse PDF / ingest zip
# -----------------------------------------------------------------------------

class ParsedArticle(BaseModel):
    article: str
    boxes: int
    net_wt_kg: float
    taxable_amount: float
    hsn_code: Optional[str] = None
    rate_per_unit: Optional[float] = None


class ParsedInvoice(BaseModel):
    invoice_no: str
    date_iso: str
    customer: str
    location: str
    po_no: Optional[str] = None
    transporter: Optional[str] = None
    lr_no: Optional[str] = None
    invoice_type: str
    total_taxable: float
    total_gross: float
    freight_charge: float
    warehouse: Optional[str] = None
    pin_code: Optional[str] = None
    e_way_bill_no: Optional[str] = None
    customer_gstin: Optional[str] = None
    payment_terms: Optional[str] = None
    approx_distance_km: Optional[int] = None
    irn: Optional[str] = None
    articles: list[ParsedArticle]


class ParsePdfRequest(BaseModel):
    """Parse a single invoice PDF and return structured data — no sheet writes."""
    pdf_path: str = Field(description="Absolute path to a single PDF file.")


class ParsePdfResponse(BaseModel):
    invoice: Optional[ParsedInvoice] = None
    skipped: bool = False
    skipped_reason: Optional[str] = None
    route: Optional[RouteName] = Field(
        default=None, description="Which tab this PDF would be written to.")


class InvoiceWriteResult(BaseModel):
    """Per-invoice row range result for a single ingest call."""
    invoice_no: str
    start_row: int
    end_row: int
    n_articles: int


class TabIngestSummary(BaseModel):
    tab: str
    invoices_seen: int
    invoices_written: int
    rows_written: int
    duplicates_skipped: int
    duplicate_invoice_nos: list[str] = Field(default_factory=list)
    po_clusters_merged: int
    written_invoices: list[InvoiceWriteResult] = Field(
        default_factory=list,
        description="Per-invoice row ranges actually written (empty in dry-run).",
    )


class IngestZipRequest(_WithSheet):
    """Parse every PDF in a zip and write new invoices to the sheet."""
    zip_path: str = Field(description="Absolute path to the zip of invoice PDFs.")
    dry_run: bool = Field(
        default=True,
        description="True (default) — preview only. False — write to sheet.",
    )
    strict: bool = Field(
        default=False,
        description="If True, parse failures cause the call to error out.",
    )


class IngestZipResponse(BaseModel):
    dry_run: bool
    parsed: int = Field(description="PDFs successfully parsed.")
    skipped_files: int = Field(description="Files skipped via classifier rules.")
    parse_errors: list[str] = Field(default_factory=list)
    by_tab: list[TabIngestSummary] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Read — list / get / find
# -----------------------------------------------------------------------------

class InvoiceOnSheetInfo(BaseModel):
    invoice_no: str
    po: Optional[str] = None
    start_row: int
    end_row: int


class ListInvoicesRequest(_WithSheet):
    tab: RouteName
    first_row: int = Field(default=2, description="First row to scan (skips header).")
    last_row: Optional[int] = Field(
        default=None,
        description="If omitted, scans through the full grid.",
    )


class ListInvoicesResponse(BaseModel):
    tab: str
    invoices: list[InvoiceOnSheetInfo]


class GetInvoiceRequest(_WithSheet):
    tab: RouteName
    invoice_no: str


class GetInvoiceResponse(BaseModel):
    found: bool
    tab: str
    invoice_no: str
    start_row: Optional[int] = None
    end_row: Optional[int] = None
    row_data: list[list[Any]] = Field(
        default_factory=list,
        description="The raw rows from the sheet (matrix; merged cells show only top-left value).",
    )


# -----------------------------------------------------------------------------
# Update — single field / cluster retrofit
# -----------------------------------------------------------------------------

# Keys we allow updating. Matches dispatch_ingest.rows.CFPL_COLS / SERVICE_COLS.
UpdatableField = Literal[
    "DATE", "CUSTOMER", "LOCATION", "PO", "BOXES", "NET_WT", "GR_WT",
    "TRANSPORTER", "LR_NO", "TRANSPORT_CHARGES", "WITHOUT_TAX",
    "TOTAL_INVOICE", "PIN_CODE", "TRANSPORT_PER_KG",
    "WAREHOUSE", "E_WAY_BILL", "CUSTOMER_GSTIN", "PAYMENT_TERMS",
    "APPROX_DISTANCE", "IRN", "HSN_CODE", "RATE_PER_UNIT", "DISPATCH_UNIT",
]


class UpdateInvoiceFieldRequest(_WithSheet):
    tab: RouteName
    invoice_no: str
    field: UpdatableField
    value: Any = Field(
        description="The new value. Numeric strings are accepted for numeric columns."
    )
    article_index: Optional[int] = Field(
        default=None,
        description="For per-article fields (Boxes/NET_WT/Article/HSN/Rate), "
                    "0-based article index. None updates the entire merged range.",
    )


class UpdateInvoiceFieldResponse(BaseModel):
    updated_range: str
    cells_updated: int


class RetrofitClustersRequest(_WithSheet):
    tab: Optional[RouteName] = Field(
        default=None,
        description="If omitted, retrofits all three target tabs.",
    )
    from_row: int = 2
    dry_run: bool = True


class ClusterMergeInfo(BaseModel):
    tab: str
    po: str
    invoices: list[str]
    start_row: int
    end_row: int


class RetrofitClustersResponse(BaseModel):
    dry_run: bool
    clusters: list[ClusterMergeInfo] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Delete
# -----------------------------------------------------------------------------

class DeleteInvoiceRequest(_WithSheet):
    tab: RouteName
    invoice_no: str
    confirm: bool = Field(
        default=False,
        description="Must be set to True to actually delete. False = dry-run.",
    )


class DeleteInvoiceResponse(BaseModel):
    dry_run: bool
    invoice_no: str
    rows_deleted: int
    deleted_range: Optional[str] = None


# -----------------------------------------------------------------------------
# DB backfill — read existing sheet rows and insert into Postgres
# -----------------------------------------------------------------------------

class BackfillRequest(_WithSheet):
    """One-off: copy invoices already on the sheet into the Postgres mirror."""
    tab: Optional[RouteName] = Field(
        default=None,
        description="If omitted, backfills all three tabs.",
    )
    first_row: int = Field(default=2, description="First row to scan (skips header).")
    last_row: Optional[int] = Field(
        default=None,
        description="If omitted, scans through the full grid.",
    )
    dry_run: bool = Field(
        default=True,
        description="True (default) — preview only. False — write to DB.",
    )


class BackfillTabResult(BaseModel):
    tab: str
    invoices_seen: int
    invoices_inserted: int
    invoices_skipped: int = Field(description="Already present in DB.")
    invoices_failed: int = Field(description="Row block could not be reconstructed.")
    errors: list[str] = Field(
        default_factory=list,
        description="Up to first 20 error messages — one per failed invoice.",
    )


class BackfillResponse(BaseModel):
    dry_run: bool
    by_tab: list[BackfillTabResult]


# -----------------------------------------------------------------------------
# Health / warm-keep — called by clients between long operations to keep the
# Lambda execution environment warm.
# -----------------------------------------------------------------------------

class HealthRequest(BaseModel):
    """No parameters — call with an empty body."""
    pass


class HealthResponse(BaseModel):
    ok: bool = True
    server: str = Field(default="dispatch-mcp-server")
    db_mirror_enabled: bool
    sku_count: int = Field(
        description="Number of SKUs loaded into the in-memory matcher cache. "
                    "Zero when DB mirror is disabled or matcher is missing.",
    )
    timestamp: str = Field(
        description="Server-side ISO-8601 UTC timestamp at the time of the call.",
    )
