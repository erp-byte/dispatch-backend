"""MCP tool registration for the dispatch service.

Tools surface the CRUD over the Dispatch Sheet:

  Setup
    - dispatch_setup_schema         — add columns + hide audit fields

  Create
    - dispatch_parse_pdf            — parse one PDF (no writes)
    - dispatch_ingest_zip           — parse a zip of PDFs and write new invoices

  Read
    - dispatch_list_invoices        — list invoice numbers + row ranges on a tab
    - dispatch_get_invoice          — get a single invoice's rows

  Update
    - dispatch_update_invoice_field — change one field of one invoice
    - dispatch_retrofit_po_clusters — merge cross-invoice shared-PO blocks

  Delete
    - dispatch_delete_invoice       — remove all rows for an invoice (soft-deletes in DB)

  Database
    - dispatch_db_backfill_from_sheet — one-off: copy sheet rows into the Postgres mirror

  Health
    - dispatch_health               — server-state ping; call between long operations
                                      to keep the Lambda warm during PDF workflows
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from services.dispatch_service.manager import DispatchManager
from services.dispatch_service.models import (
    BackfillRequest, BackfillResponse,
    DeleteInvoiceRequest, DeleteInvoiceResponse,
    GetInvoiceRequest, GetInvoiceResponse,
    HealthRequest, HealthResponse,
    IngestZipRequest, IngestZipResponse,
    ListInvoicesRequest, ListInvoicesResponse,
    ParsePdfRequest, ParsePdfResponse,
    RetrofitClustersRequest, RetrofitClustersResponse,
    SetupSchemaRequest, SetupSchemaResponse,
    UpdateInvoiceFieldRequest, UpdateInvoiceFieldResponse,
)


def register(server: FastMCP, mgr: DispatchManager) -> None:
    @server.tool()
    async def dispatch_setup_schema(req: SetupSchemaRequest) -> SetupSchemaResponse:
        """Ensure all dispatch column headers exist on a tab and configured
        audit columns are hidden. Idempotent — safe to call repeatedly."""
        return await mgr.setup_schema(req)

    @server.tool()
    async def dispatch_parse_pdf(req: ParsePdfRequest) -> ParsePdfResponse:
        """Parse a single invoice PDF and return structured fields. No writes."""
        return await mgr.parse_pdf(req)

    @server.tool()
    async def dispatch_ingest_zip(req: IngestZipRequest) -> IngestZipResponse:
        """Parse every invoice PDF in a zip and write the new ones to the sheet.

        Default is `dry_run=True` (preview only). Set `dry_run=False` to commit.
        Duplicates (same Invoice No already on the sheet) are skipped.
        Shared-PO clusters are auto-merged across consecutive invoices.
        """
        return await mgr.ingest_zip(req)

    @server.tool()
    async def dispatch_list_invoices(req: ListInvoicesRequest) -> ListInvoicesResponse:
        """List invoice numbers and their row ranges within a row window on a tab."""
        return await mgr.list_invoices(req)

    @server.tool()
    async def dispatch_get_invoice(req: GetInvoiceRequest) -> GetInvoiceResponse:
        """Get the row data for a specific invoice on a tab."""
        return await mgr.get_invoice(req)

    @server.tool()
    async def dispatch_update_invoice_field(
        req: UpdateInvoiceFieldRequest,
    ) -> UpdateInvoiceFieldResponse:
        """Update a single field on a specific invoice. Routes through sheets hooks.

        For per-article fields (Boxes/NET_WT/Article/HSN/Rate), set `article_index`
        to the 0-based index of the article row to target. For invoice-level fields
        (DATE/CUSTOMER/LOCATION/PO/WAREHOUSE/etc.), leave `article_index` unset
        and the update is applied to the merged range.
        """
        return await mgr.update_invoice_field(req)

    @server.tool()
    async def dispatch_retrofit_po_clusters(
        req: RetrofitClustersRequest,
    ) -> RetrofitClustersResponse:
        """Find consecutive invoices that share a PO and merge the PO column across them.

        Default is `dry_run=True` — returns the clusters found without writing.
        """
        return await mgr.retrofit_po_clusters(req)

    @server.tool()
    async def dispatch_delete_invoice(req: DeleteInvoiceRequest) -> DeleteInvoiceResponse:
        """Delete every row belonging to an invoice (and unmerge any merges first).

        Default is `confirm=False` (dry-run). Set `confirm=True` to actually delete.
        """
        return await mgr.delete_invoice(req)

    @server.tool()
    async def dispatch_db_backfill_from_sheet(
        req: BackfillRequest,
    ) -> BackfillResponse:
        """One-off: copy existing sheet rows into the Postgres mirror.

        Idempotent — invoices already present (by route + invoice_no) are skipped.
        Default ``dry_run=True`` previews counts only. Set ``dry_run=False`` to write.
        Requires DATABASE_URL to be set in the server environment.
        """
        return await mgr.backfill_from_sheet(req)

    @server.tool()
    async def dispatch_health(req: HealthRequest) -> HealthResponse:
        """Lightweight server-state ping. Use during long PDF-processing workflows
        to keep the Lambda execution environment warm.

        WHEN TO CALL:
          - Between parse_pdf / ingest_zip calls if you're working through many
            PDFs in sequence (e.g., call once every 1-2 minutes during a batch).
          - Before resuming after a long pause where the user was reviewing
            output (~3+ minutes idle).
          - As a sanity check that the DB mirror is up (db_mirror_enabled=true
            means writes will land in Postgres in addition to the sheet).

        DO NOT CALL:
          - Between every tool call — that's noise.
          - For correctness — sheet/DB writes already succeed without it.

        Returns server-state: ok flag, DB mirror status, loaded SKU count,
        and a server-side UTC timestamp. No side effects other than refreshing
        the warm-pool clock.
        """
        return await mgr.health(req)
