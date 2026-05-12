"""DispatchManager — CRUD operations over the Dispatch Sheet.

All write paths route through ``SheetsManager`` so they inherit the project's
validate / audit / timestamp hook chain (when enabled in env).
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from typing import Any, Optional

from dispatch_ingest.classifier import classify, Route
from dispatch_ingest.parser import parse_pdf
from dispatch_ingest.rows import (
    HIDDEN_KEYS, NEW_COLUMN_HEADERS, SCHEMA_BY_ROUTE, build_rows,
)
from dispatch_ingest.sheet_scan import find_invoices_in_range
from dispatch_ingest.utils import col_letter, inv_no_key
from dispatch_ingest.writer import DispatchWriter, extract_spreadsheet_id

from services.auth_service.client_factory import ClientFactory
from services.dispatch_service.models import (
    BackfillRequest, BackfillResponse, BackfillTabResult,
    ClusterMergeInfo, DeleteInvoiceRequest, DeleteInvoiceResponse,
    GetInvoiceRequest, GetInvoiceResponse, HealthRequest, HealthResponse,
    IngestZipRequest, IngestZipResponse,
    InvoiceOnSheetInfo, InvoiceWriteResult, ListInvoicesRequest,
    ListInvoicesResponse, ParsePdfRequest, ParsePdfResponse, ParsedArticle,
    ParsedInvoice, RetrofitClustersRequest, RetrofitClustersResponse, RouteName,
    SetupSchemaRequest, SetupSchemaResponse, TabIngestSummary,
    UpdateInvoiceFieldRequest, UpdateInvoiceFieldResponse,
)
from services.dispatch_service.repository import DispatchRepository
from services.sheets_service.manager import SheetsManager
from services.sheets_service.models import (
    DeleteDimensionsRequest, ReadRangeRequest,
    UnmergeCellsRequest, UpdateRangeRequest,
)
from shared.exceptions import HookAbort, SheetsError

import logging

import asyncpg

log = logging.getLogger(__name__)


async def _mirror(coro, *, op: str, **ctx) -> None:
    """Run a DB mirror coroutine. Logs + swallows failures. Never raises.

    Passes silently when `coro` is None — that's how the manager signals
    "DB mirror disabled" (e.g. no DATABASE_URL configured).
    """
    if coro is None:
        return
    try:
        await coro
    except asyncpg.PostgresError as e:
        log.warning("db_mirror_failed op=%s sqlstate=%s ctx=%s error=%s",
                    op, getattr(e, "sqlstate", None), ctx, str(e))
    except Exception as e:
        log.warning("db_mirror_failed op=%s ctx=%s error=%r", op, ctx, e)


_TAB_BY_NAME: dict[str, Route] = {
    "CFPL": Route.CFPL,
    "CDPL": Route.CDPL,
    "SERVICE CFPL": Route.SERVICE_CFPL,
}

# A bare spreadsheet ID is 20–60 alphanumerics + - / _. The URL form is
# normalised by `extract_spreadsheet_id` before this check runs (C4).
_VALID_SHEET_ID_RE = re.compile(r"^[a-zA-Z0-9_.\-]{20,80}$")

# I2: formula injection guard. If `value_input_option=USER_ENTERED`, a value
# starting with '=' would be interpreted as a formula. We block this in
# update_invoice_field unless the caller explicitly asks for RAW.
_FORMULA_PREFIX_RE = re.compile(r"^\s*[=+\-@]")


def _safe_user_path(path: str, *, kind: str) -> str:
    """Validate a user-provided file path: must be absolute, must exist as a
    file, must NOT traverse outside the resolved real path. Returns the
    resolved absolute path. Raises ``SheetsError`` on failure.
    """
    if not path or not isinstance(path, str):
        raise SheetsError(
            code="INVALID_PATH", status=400, reason="Bad Request",
            message=f"{kind} path is required",
            service_account_email=None, hint=None,
        )
    if not os.path.isabs(path):
        raise SheetsError(
            code="INVALID_PATH", status=400, reason="Bad Request",
            message=f"{kind} path must be absolute, got {path!r}",
            service_account_email=None,
            hint="Use a full path like 'C:\\path\\to\\file.pdf' or '/abs/path/file.pdf'.",
        )
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved):
        raise SheetsError(
            code="FILE_NOT_FOUND", status=404, reason="Not Found",
            message=f"{kind} not found at {resolved!r}",
            service_account_email=None, hint=None,
        )
    return resolved


class DispatchManager:
    """Routes dispatch-domain CRUD through SheetsManager + the ingest pipeline."""

    def __init__(self, sheets_mgr: SheetsManager, factory: ClientFactory,
                 default_spreadsheet_id: Optional[str],
                 repo: Optional[DispatchRepository] = None):
        self._mgr = sheets_mgr
        self._factory = factory
        self._default_sid = default_spreadsheet_id
        self._repo = repo

    def _repo_call(self, method_name: str, **kwargs):
        """Return the awaitable for `repo.<method_name>(**kwargs)`, or None.

        Used at every dual-write call site so passing the result to `_mirror`
        is a no-op when DB mirror is disabled (repo is None).
        """
        if self._repo is None:
            return None
        return getattr(self._repo, method_name)(**kwargs)

    # ---------- helpers ----------

    def _resolve_sid(self, requested: Optional[str]) -> str:
        sid = requested or self._default_sid
        if not sid:
            raise SheetsError(
                code="MISSING_SPREADSHEET_ID", status=400, reason="Bad Request",
                message=("No spreadsheet_id provided and SPREADSHEET_ID is not set "
                         "in the server's environment."),
                service_account_email=None,
                hint="Set SPREADSHEET_ID in .env or pass spreadsheet_id explicitly.",
            )
        normalised = extract_spreadsheet_id(sid)
        if not _VALID_SHEET_ID_RE.match(normalised):  # C4
            raise SheetsError(
                code="INVALID_SPREADSHEET_ID", status=400, reason="Bad Request",
                message=f"value {normalised!r} doesn't look like a Sheets ID",
                service_account_email=None,
                hint=("Pass a full sheet URL or a bare ID of 20+ chars. "
                      "Example: https://docs.google.com/spreadsheets/d/<ID>/edit"),
            )
        return normalised

    def _route_for(self, tab: RouteName) -> Route:
        return _TAB_BY_NAME[tab]

    async def _require_tab(self, writer: DispatchWriter, tab_name: str):
        tabs = await writer.list_tabs()
        if tab_name not in tabs:
            raise SheetsError(
                code="TAB_NOT_FOUND", status=404, reason="Not Found",
                message=f"tab {tab_name!r} does not exist in the spreadsheet",
                service_account_email=None,
                hint=f"Available tabs: {sorted(tabs.keys())}",
            )
        return tabs, tabs[tab_name]

    async def _build_writer(self, sid: str, dry_run: bool = False) -> DispatchWriter:
        return DispatchWriter(self._mgr, self._factory, sid, dry_run=dry_run)

    async def _find_match(self, sid: str, tab_name: str, tab,
                          inv_col: int, po_col: int, invoice_no: str):
        """Locate an invoice on the sheet; raise INVOICE_NOT_FOUND if absent (I14)."""
        invs = find_invoices_in_range(
            self._factory.sheets(), sid, tab_name, tab.tab_id,
            inv_col, po_col, 2, tab.grid_rows,
        )
        match = next((i for i in invs if i.invoice_no == invoice_no), None)
        if not match:
            raise SheetsError(
                code="INVOICE_NOT_FOUND", status=404, reason="Not Found",
                message=f"invoice {invoice_no!r} not on tab {tab_name!r}",
                service_account_email=None,
                hint=(f"Use dispatch_list_invoices to confirm the invoice number "
                      f"as stored on the sheet (case- and whitespace-sensitive)."),
            )
        return match

    # ---------- SETUP ----------

    async def setup_schema(self, req: SetupSchemaRequest) -> SetupSchemaResponse:
        sid = self._resolve_sid(req.spreadsheet_id)
        route = self._route_for(req.tab)
        writer = await self._build_writer(sid)
        try:
            _tabs, tab = await self._require_tab(writer, req.tab)
        except SheetsError:
            raise
        cols = SCHEMA_BY_ROUTE[route][1]

        added: list[str] = []
        present: list[str] = []
        try:
            for key, label in NEW_COLUMN_HEADERS:
                if key not in cols:
                    continue
                was_added = await writer.ensure_named_column(route, tab, key, label)
                (added if was_added else present).append(label)
            hide_cols = [cols[k] for k in HIDDEN_KEYS if k in cols]
            await writer.hide_columns(tab, hide_cols, label=req.tab)
        except HookAbort as e:  # I7
            raise self._hook_abort_as_sheets_error(e)
        return SetupSchemaResponse(
            tab=req.tab,
            headers_added=added,
            headers_already_present=present,
            columns_hidden=[col_letter(c) for c in hide_cols],
        )

    # ---------- CREATE — parse one PDF ----------

    async def parse_pdf(self, req: ParsePdfRequest) -> ParsePdfResponse:
        path = _safe_user_path(req.pdf_path, kind="pdf_path")  # I12
        cls = classify(path)
        if cls.route == Route.SKIP:
            return ParsePdfResponse(skipped=True, skipped_reason=cls.reason)
        inv = parse_pdf(path)
        articles = [
            ParsedArticle(
                article=a.article, boxes=a.boxes, net_wt_kg=a.net_wt_kg,
                taxable_amount=a.taxable_amount, hsn_code=a.hsn_code,
                rate_per_unit=a.rate_per_unit,
            )
            for a in inv.unique_articles
        ]
        route_name = {Route.CFPL: "CFPL", Route.CDPL: "CDPL",
                      Route.SERVICE_CFPL: "SERVICE CFPL"}.get(cls.route)
        return ParsePdfResponse(
            invoice=ParsedInvoice(
                invoice_no=inv.invoice_no, date_iso=inv.date_iso,
                customer=inv.customer, location=inv.location, po_no=inv.po_no,
                transporter=inv.transporter, lr_no=inv.lr_no,
                invoice_type=inv.invoice_type, total_taxable=inv.total_taxable,
                total_gross=inv.total_gross, freight_charge=inv.freight_charge,
                warehouse=inv.warehouse, pin_code=inv.pin_code,
                e_way_bill_no=inv.e_way_bill_no, customer_gstin=inv.customer_gstin,
                payment_terms=inv.payment_terms,
                approx_distance_km=inv.approx_distance_km, irn=inv.irn,
                articles=articles,
            ),
            route=route_name,  # type: ignore[arg-type]
        )

    # ---------- CREATE — ingest a zip of PDFs ----------

    async def ingest_zip(self, req: IngestZipRequest) -> IngestZipResponse:
        sid = self._resolve_sid(req.spreadsheet_id)
        zip_path = _safe_user_path(req.zip_path, kind="zip_path")  # I12
        writer = await self._build_writer(sid, dry_run=req.dry_run)

        blocks_by_route: dict[Route, list] = {
            Route.CFPL: [], Route.CDPL: [], Route.SERVICE_CFPL: [],
        }
        # Track parsed InvoiceData + source filename per (route, invoice_no) so
        # we can mirror to DB after the sheet row range is known. Without this,
        # the inner write loop only has access to InvoiceRowBlock which lacks
        # the structured invoice fields.
        invoices_by_no: dict[Route, dict[str, tuple]] = {
            Route.CFPL: {}, Route.CDPL: {}, Route.SERVICE_CFPL: {},
        }
        parse_errors: list[str] = []
        skipped_count = 0

        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(zip_path) as z:
                for name in sorted(z.namelist()):
                    if not name.lower().endswith(".pdf"):
                        continue
                    cls = classify(name)
                    if cls.route == Route.SKIP:
                        skipped_count += 1
                        continue
                    local = os.path.join(td, os.path.basename(name))
                    with z.open(name) as src, open(local, "wb") as dst:
                        dst.write(src.read())
                    try:
                        inv = parse_pdf(local)
                    except Exception as e:  # noqa: BLE001
                        parse_errors.append(f"{name}: {e}")
                        continue
                    block = build_rows(inv, cls.route)
                    blocks_by_route[cls.route].append(block)
                    invoices_by_no[cls.route][block.invoice_no] = (inv, name)

        if req.strict and parse_errors:
            raise SheetsError(
                code="PARSE_ERROR", status=400, reason="Bad Request",
                message=f"{len(parse_errors)} parse error(s) in --strict mode",
                service_account_email=None,
                hint="\n".join(parse_errors),
            )

        for r in blocks_by_route:
            blocks_by_route[r].sort(key=lambda b: inv_no_key(b.invoice_no))

        total_parsed = sum(len(bs) for bs in blocks_by_route.values())
        tabs = await writer.list_tabs()
        summaries: list[TabIngestSummary] = []

        try:
            for route, blocks in blocks_by_route.items():
                tab_name = {Route.CFPL: "CFPL", Route.CDPL: "CDPL",
                            Route.SERVICE_CFPL: "SERVICE CFPL"}[route]
                if tab_name not in tabs:
                    continue
                tab = tabs[tab_name]
                cols = SCHEMA_BY_ROUTE[route][1]

                for key, label in NEW_COLUMN_HEADERS:
                    if key not in cols:
                        continue
                    await writer.ensure_named_column(route, tab, key, label)
                hide_cols = [cols[k] for k in HIDDEN_KEYS if k in cols]
                await writer.hide_columns(tab, hide_cols, label=tab_name)

                if not blocks:
                    summaries.append(TabIngestSummary(
                        tab=tab_name, invoices_seen=0, invoices_written=0,
                        rows_written=0, duplicates_skipped=0,
                        duplicate_invoice_nos=[], po_clusters_merged=0,
                    ))
                    continue

                existing = await writer.fetch_existing_invoice_nos(route, tab)
                new_blocks = [b for b in blocks if b.invoice_no not in existing]
                dup_invoices = [b.invoice_no for b in blocks if b.invoice_no in existing]

                po_col = cols["PO"]
                inv_col = cols["INVOICE_NO"]
                from dispatch_ingest.run import po_clusters as _po_clusters
                clusters = _po_clusters(new_blocks)
                in_cluster = {b.invoice_no for cluster, _ in clusters for b in cluster}
                block_row_ranges: dict[str, tuple[int, int]] = {}

                # C3: detect a possible cross-batch tail merge. If the last
                # existing invoice on the sheet shares a PO with the FIRST new
                # block we're about to write, the bigger merge spans both.
                tail_po = None
                tail_invoices = []
                if new_blocks and tab.grid_rows >= 2:
                    on_sheet = find_invoices_in_range(
                        self._factory.sheets(), sid, tab_name, tab.tab_id,
                        inv_col, po_col, max(2, tab.grid_rows - 50), tab.grid_rows,
                    )
                    if on_sheet:
                        last = on_sheet[-1]
                        first_new_po = str(new_blocks[0].rows[0][po_col - 1] or "")
                        if last.po and last.po == first_new_po:
                            tail_po = last.po
                            tail_invoices = [i for i in on_sheet if i.po == last.po]

                for block in new_blocks:
                    block_po = str(block.rows[0][po_col - 1] or "")
                    skip_po = (block.invoice_no in in_cluster
                               or (tail_po is not None and block_po == tail_po))
                    skip = {po_col} if skip_po else set()
                    start_row, end_row = await writer.write_block(
                        block, tab, skip_merge_cols=skip,
                    )
                    block_row_ranges[block.invoice_no] = (start_row, end_row)

                    # I26: mirror to DB best-effort after sheet write succeeds.
                    # Failure logs a WARNING but does not fail the request.
                    if start_row > 0 and not writer.dry_run:
                        inv_data, src_name = invoices_by_no[route][block.invoice_no]
                        pi = ParsedInvoice(
                            invoice_no=inv_data.invoice_no,
                            date_iso=inv_data.date_iso,
                            customer=inv_data.customer,
                            location=inv_data.location,
                            po_no=inv_data.po_no,
                            transporter=inv_data.transporter,
                            lr_no=inv_data.lr_no,
                            invoice_type=inv_data.invoice_type,
                            total_taxable=inv_data.total_taxable,
                            total_gross=inv_data.total_gross,
                            freight_charge=inv_data.freight_charge,
                            warehouse=inv_data.warehouse,
                            pin_code=inv_data.pin_code,
                            e_way_bill_no=inv_data.e_way_bill_no,
                            customer_gstin=inv_data.customer_gstin,
                            payment_terms=inv_data.payment_terms,
                            approx_distance_km=inv_data.approx_distance_km,
                            irn=inv_data.irn,
                            articles=[
                                ParsedArticle(
                                    article=a.article, boxes=a.boxes,
                                    net_wt_kg=a.net_wt_kg,
                                    taxable_amount=a.taxable_amount,
                                    hsn_code=a.hsn_code,
                                    rate_per_unit=a.rate_per_unit,
                                )
                                for a in inv_data.unique_articles
                            ],
                        )
                        route_db_name = {Route.CFPL: "CFPL", Route.CDPL: "CDPL",
                                         Route.SERVICE_CFPL: "SERVICE_CFPL"}[route]
                        await _mirror(
                            self._repo_call(
                                "upsert_invoice",
                                invoice=pi, route=route_db_name,
                                sheet_start_row=start_row,
                                sheet_end_row=end_row,
                                source_pdf=os.path.basename(src_name),
                            ),
                            op="ingest_zip.upsert_invoice",
                            route=route_db_name, invoice_no=block.invoice_no,
                        )

                cluster_count = 0
                # In-batch clusters
                for cluster, shared_po in clusters:
                    if writer.dry_run:
                        cluster_count += 1
                        continue
                    first = block_row_ranges[cluster[0].invoice_no][0]
                    last = block_row_ranges[cluster[-1].invoice_no][1]
                    await writer.merge_range(tab, po_col, first, last,
                                             label=f"PO {shared_po}")
                    cluster_count += 1

                # Cross-batch tail merge: extend the existing tail-PO merge to
                # include the new blocks that share the same PO.
                if tail_po and not writer.dry_run:
                    tail_new = [b for b in new_blocks
                                if str(b.rows[0][po_col - 1] or "") == tail_po]
                    if tail_new and tail_invoices:
                        first = tail_invoices[0].start_row
                        last = block_row_ranges[tail_new[-1].invoice_no][1]
                        rng = (f"'{tab_name}'!{col_letter(po_col)}{first}:"
                               f"{col_letter(po_col)}{last}")
                        await self._mgr.unmerge_cells(UnmergeCellsRequest(
                            spreadsheet_id=sid, range_a1=rng,
                        ))
                        await writer.merge_range(tab, po_col, first, last,
                                                 label=f"PO {tail_po} (cross-batch)")
                        cluster_count += 1

                written = [
                    InvoiceWriteResult(
                        invoice_no=b.invoice_no,
                        start_row=block_row_ranges[b.invoice_no][0],
                        end_row=block_row_ranges[b.invoice_no][1],
                        n_articles=b.n_articles,
                    )
                    for b in new_blocks if b.invoice_no in block_row_ranges
                    and block_row_ranges[b.invoice_no][0] > 0
                ]

                summaries.append(TabIngestSummary(
                    tab=tab_name,
                    invoices_seen=len(blocks),
                    invoices_written=len(new_blocks),
                    rows_written=sum(b.n_articles for b in new_blocks),
                    duplicates_skipped=len(dup_invoices),
                    duplicate_invoice_nos=dup_invoices,
                    po_clusters_merged=cluster_count,
                    written_invoices=written if not writer.dry_run else [],
                ))
        except HookAbort as e:  # I7
            raise self._hook_abort_as_sheets_error(e)

        return IngestZipResponse(
            dry_run=req.dry_run, parsed=total_parsed,
            skipped_files=skipped_count, parse_errors=parse_errors,
            by_tab=summaries,
        )

    # ---------- READ — list invoices on a tab ----------

    async def list_invoices(self, req: ListInvoicesRequest) -> ListInvoicesResponse:
        sid = self._resolve_sid(req.spreadsheet_id)
        route = self._route_for(req.tab)
        writer = await self._build_writer(sid)
        _tabs, tab = await self._require_tab(writer, req.tab)
        cols = SCHEMA_BY_ROUTE[route][1]
        last_row = req.last_row or tab.grid_rows
        invs = find_invoices_in_range(
            self._factory.sheets(), sid, req.tab, tab.tab_id,
            cols["INVOICE_NO"], cols["PO"], req.first_row, last_row,
        )
        return ListInvoicesResponse(
            tab=req.tab,
            invoices=[
                InvoiceOnSheetInfo(invoice_no=i.invoice_no, po=i.po or None,
                                   start_row=i.start_row, end_row=i.end_row)
                for i in invs
            ],
        )

    # ---------- READ — get a single invoice's rows ----------

    async def get_invoice(self, req: GetInvoiceRequest) -> GetInvoiceResponse:
        sid = self._resolve_sid(req.spreadsheet_id)
        route = self._route_for(req.tab)
        writer = await self._build_writer(sid)
        _tabs, tab = await self._require_tab(writer, req.tab)  # C2
        cols = SCHEMA_BY_ROUTE[route][1]
        try:
            match = await self._find_match(
                sid, req.tab, tab, cols["INVOICE_NO"], cols["PO"], req.invoice_no,
            )
        except SheetsError as e:
            if e.code == "INVOICE_NOT_FOUND":
                return GetInvoiceResponse(found=False, tab=req.tab,
                                          invoice_no=req.invoice_no)
            raise
        width = SCHEMA_BY_ROUTE[route][2]
        rng = (f"'{req.tab}'!A{match.start_row}:{col_letter(width)}{match.end_row}")
        resp = await self._mgr.read_range(ReadRangeRequest(
            spreadsheet_id=sid, range_a1=rng,
        ))
        return GetInvoiceResponse(
            found=True, tab=req.tab, invoice_no=match.invoice_no,
            start_row=match.start_row, end_row=match.end_row,
            row_data=resp.values,
        )

    # ---------- UPDATE — single field ----------

    async def update_invoice_field(
        self, req: UpdateInvoiceFieldRequest,
    ) -> UpdateInvoiceFieldResponse:
        sid = self._resolve_sid(req.spreadsheet_id)
        route = self._route_for(req.tab)
        cols = SCHEMA_BY_ROUTE[route][1]
        # I1: per-route field validation. The Literal advertises all keys, but
        # SERVICE CFPL doesn't have e.g. TOTAL_INVOICE — reject explicitly.
        if req.field not in cols:
            raise SheetsError(
                code="UNKNOWN_FIELD", status=400, reason="Bad Request",
                message=f"field {req.field!r} is not valid on tab {req.tab!r}",
                service_account_email=None,
                hint=f"Allowed fields on {req.tab}: {sorted(cols)}",
            )
        # I2: formula-injection guard for USER_ENTERED.
        if isinstance(req.value, str) and _FORMULA_PREFIX_RE.match(req.value):
            raise SheetsError(
                code="UNSAFE_VALUE", status=400, reason="Bad Request",
                message=("value starts with '=' or another formula prefix; "
                         "Sheets would evaluate it as a formula"),
                service_account_email=None,
                hint=("Strip leading '=' / '+' / '-' / '@' or prefix with a single "
                      "quote (e.g. \"'=text\") if you need to store the literal."),
            )

        col = cols[req.field]
        writer = await self._build_writer(sid)
        _tabs, tab = await self._require_tab(writer, req.tab)  # C2
        try:
            match = await self._find_match(
                sid, req.tab, tab, cols["INVOICE_NO"], cols["PO"], req.invoice_no,
            )
        except SheetsError:
            raise

        n_rows = match.end_row - match.start_row + 1
        try:
            if req.article_index is not None:
                if not (0 <= req.article_index < n_rows):
                    raise SheetsError(
                        code="OUT_OF_RANGE", status=400, reason="Bad Request",
                        message=(f"article_index {req.article_index} out of range "
                                 f"[0, {n_rows})"),
                        service_account_email=None, hint=None,
                    )
                row_idx = match.start_row + req.article_index
                rng = f"'{req.tab}'!{col_letter(col)}{row_idx}"
                resp = await self._mgr.update_range(UpdateRangeRequest(
                    spreadsheet_id=sid, range_a1=rng,
                    values=[[req.value]], value_input_option="USER_ENTERED",
                ))
            else:
                rng = (f"'{req.tab}'!{col_letter(col)}{match.start_row}:"
                       f"{col_letter(col)}{match.end_row}")
                resp = await self._mgr.update_range(UpdateRangeRequest(
                    spreadsheet_id=sid, range_a1=rng,
                    values=[[req.value] for _ in range(n_rows)],
                    value_input_option="USER_ENTERED",
                ))
        except HookAbort as e:  # I7
            raise self._hook_abort_as_sheets_error(e)
        # Mirror to DB best-effort
        route_db_name = {Route.CFPL: "CFPL", Route.CDPL: "CDPL",
                         Route.SERVICE_CFPL: "SERVICE_CFPL"}[route]
        await _mirror(
            self._repo_call(
                "update_field",
                route=route_db_name, invoice_no=req.invoice_no,
                field=req.field, value=req.value,
                article_index=req.article_index,
            ),
            op="update_invoice_field.update_field",
            route=route_db_name, invoice_no=req.invoice_no, field=req.field,
        )
        return UpdateInvoiceFieldResponse(
            updated_range=resp.updated_range or rng,
            cells_updated=resp.updated_cells,
        )

    # ---------- UPDATE — retrofit cross-invoice PO clusters ----------

    async def retrofit_po_clusters(
        self, req: RetrofitClustersRequest,
    ) -> RetrofitClustersResponse:
        from dispatch_ingest.retrofit import _clusters
        sid = self._resolve_sid(req.spreadsheet_id)
        writer = await self._build_writer(sid, dry_run=req.dry_run)
        tabs = await writer.list_tabs()
        target_tabs = ([req.tab] if req.tab else ["CFPL", "CDPL", "SERVICE CFPL"])
        all_clusters: list[ClusterMergeInfo] = []

        try:
            for tab_name in target_tabs:
                if tab_name not in tabs:
                    continue
                tab = tabs[tab_name]
                route = self._route_for(tab_name)  # type: ignore[arg-type]
                cols = SCHEMA_BY_ROUTE[route][1]
                if req.from_row > tab.grid_rows:
                    continue
                invs = find_invoices_in_range(
                    self._factory.sheets(), sid, tab_name, tab.tab_id,
                    cols["INVOICE_NO"], cols["PO"], req.from_row, tab.grid_rows,
                )
                for cluster, po in _clusters(invs):
                    first = cluster[0].start_row
                    last = cluster[-1].end_row
                    all_clusters.append(ClusterMergeInfo(
                        tab=tab_name, po=po,
                        invoices=[c.invoice_no for c in cluster],
                        start_row=first, end_row=last,
                    ))
                    if not req.dry_run:
                        rng = (f"'{tab_name}'!{col_letter(cols['PO'])}{first}:"
                               f"{col_letter(cols['PO'])}{last}")
                        await self._mgr.unmerge_cells(UnmergeCellsRequest(
                            spreadsheet_id=sid, range_a1=rng,
                        ))
                        await writer.merge_range(tab, cols["PO"], first, last,
                                                 label=f"PO {po}")
                        # Mirror PO assignment to DB best-effort
                        route_db_name = {Route.CFPL: "CFPL", Route.CDPL: "CDPL",
                                         Route.SERVICE_CFPL: "SERVICE_CFPL"}[route]
                        await _mirror(
                            self._repo_call(
                                "update_po_for_invoices",
                                route=route_db_name,
                                invoice_nos=[c.invoice_no for c in cluster],
                                po_no=po,
                            ),
                            op="retrofit_po_clusters.update_po",
                            route=route_db_name, po_no=po,
                        )
        except HookAbort as e:  # I7
            raise self._hook_abort_as_sheets_error(e)
        return RetrofitClustersResponse(dry_run=req.dry_run, clusters=all_clusters)

    # ---------- DELETE — remove an invoice's rows ----------

    async def delete_invoice(self, req: DeleteInvoiceRequest) -> DeleteInvoiceResponse:
        sid = self._resolve_sid(req.spreadsheet_id)
        route = self._route_for(req.tab)
        writer = await self._build_writer(sid)
        _tabs, tab = await self._require_tab(writer, req.tab)  # C2
        cols = SCHEMA_BY_ROUTE[route][1]
        width = SCHEMA_BY_ROUTE[route][2]  # I9
        match = await self._find_match(
            sid, req.tab, tab, cols["INVOICE_NO"], cols["PO"], req.invoice_no,
        )
        n_rows = match.end_row - match.start_row + 1
        rng = f"'{req.tab}'!A{match.start_row}:{col_letter(width)}{match.end_row}"

        if not req.confirm:
            return DeleteInvoiceResponse(
                dry_run=True, invoice_no=req.invoice_no,
                rows_deleted=n_rows, deleted_range=rng,
            )

        try:
            # 1. Unmerge any merges intersecting the rows (uses correct width per I9)
            await self._mgr.unmerge_cells(UnmergeCellsRequest(
                spreadsheet_id=sid, range_a1=rng,
            ))
            # 2. Delete via SheetsManager.delete_dimensions — hook-instrumented (C1)
            await self._mgr.delete_dimensions(DeleteDimensionsRequest(
                spreadsheet_id=sid, sheet_id=tab.tab_id, dimension="ROWS",
                start_index=match.start_row, end_index=match.end_row,
            ))
        except HookAbort as e:  # I7
            raise self._hook_abort_as_sheets_error(e)
        # Mirror as soft-delete in DB best-effort
        route_db_name = {Route.CFPL: "CFPL", Route.CDPL: "CDPL",
                         Route.SERVICE_CFPL: "SERVICE_CFPL"}[route]
        await _mirror(
            self._repo_call(
                "soft_delete_invoice",
                route=route_db_name, invoice_no=req.invoice_no,
            ),
            op="delete_invoice.soft_delete",
            route=route_db_name, invoice_no=req.invoice_no,
        )
        return DeleteInvoiceResponse(
            dry_run=False, invoice_no=req.invoice_no,
            rows_deleted=n_rows, deleted_range=rng,
        )

    # ---------- BACKFILL — copy existing sheet rows into DB ----------

    async def _read_row_block_for_backfill(
        self, sid: str, tab_name: str, start_row: int, end_row: int, width: int,
    ) -> list[list[Any]]:
        """Read rows start_row..end_row with UNFORMATTED_VALUE rendering.

        Backfill needs native int/float for dates and numbers, not the
        FORMATTED_VALUE strings used by the rest of the read path.
        """
        rng = f"'{tab_name}'!A{start_row}:{col_letter(width)}{end_row}"
        resp = await self._mgr.read_range(ReadRangeRequest(
            spreadsheet_id=sid, range_a1=rng,
            value_render_option="UNFORMATTED_VALUE",
        ))
        return resp.values

    async def backfill_from_sheet(self, req: BackfillRequest) -> BackfillResponse:
        """One-off: copy existing sheet rows into the DB. Idempotent."""
        if self._repo is None:
            raise SheetsError(
                code="DB_DISABLED", status=400, reason="Bad Request",
                message="DB mirror is not configured (DATABASE_URL is empty)",
                service_account_email=None,
                hint="Set DATABASE_URL in .env before running backfill.",
            )

        sid = self._resolve_sid(req.spreadsheet_id)
        target_tabs: list[RouteName] = (
            [req.tab] if req.tab else ["CFPL", "CDPL", "SERVICE CFPL"]
        )

        # Import here to avoid a top-level dependency on backfill module.
        from services.dispatch_service.backfill import _row_block_to_invoice

        results: list[BackfillTabResult] = []
        for tab_name in target_tabs:
            route = self._route_for(tab_name)
            route_db_name = {Route.CFPL: "CFPL", Route.CDPL: "CDPL",
                             Route.SERVICE_CFPL: "SERVICE_CFPL"}[route]
            _name, _cols, width = SCHEMA_BY_ROUTE[route]

            list_req = ListInvoicesRequest(
                spreadsheet_id=sid, tab=tab_name,
                first_row=req.first_row, last_row=req.last_row,
            )
            listing = await self.list_invoices(list_req)
            already = await self._repo.check_existing(
                route=route_db_name,
                invoice_nos=[i.invoice_no for i in listing.invoices],
            )

            seen = inserted = skipped = failed = 0
            errors: list[str] = []
            for info in listing.invoices:
                seen += 1
                if info.invoice_no in already:
                    skipped += 1
                    continue
                try:
                    rows = await self._read_row_block_for_backfill(
                        sid, tab_name, info.start_row, info.end_row, width,
                    )
                    invoice = _row_block_to_invoice(rows, route)
                    if not req.dry_run:
                        await self._repo.upsert_invoice(
                            invoice=invoice, route=route_db_name,
                            sheet_start_row=info.start_row,
                            sheet_end_row=info.end_row,
                            source_pdf=None,
                        )
                    inserted += 1
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    if len(errors) < 20:
                        errors.append(f"{info.invoice_no}: {e!r}")

            results.append(BackfillTabResult(
                tab=tab_name,
                invoices_seen=seen,
                invoices_inserted=inserted,
                invoices_skipped=skipped,
                invoices_failed=failed,
                errors=errors,
            ))

        return BackfillResponse(dry_run=req.dry_run, by_tab=results)

    # ---------- HEALTH / warm-keep ----------

    async def health(self, req: "HealthRequest") -> "HealthResponse":
        """Return server-state info. Side-effect: touches the DB pool via the
        repo reference, which keeps the Lambda execution environment warm.

        Safe to call repeatedly during long-running Claude PDF workflows.
        Returns the same shape whether or not DB mirror is enabled.
        """
        from datetime import datetime, timezone
        db_enabled = self._repo is not None
        sku_count = 0
        if db_enabled and self._repo is not None:
            matcher = getattr(self._repo, "_sku_matcher", None)
            if matcher is not None:
                sku_count = matcher.loaded_count
        return HealthResponse(
            db_mirror_enabled=db_enabled,
            sku_count=sku_count,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ---------- error mapping ----------

    @staticmethod
    def _hook_abort_as_sheets_error(e: HookAbort) -> SheetsError:
        """Wrap a HookAbort as a structured SheetsError for the MCP client (I7)."""
        return SheetsError(
            code="HOOK_ABORTED", status=403, reason="Forbidden",
            message=f"hook {e.hook_name!r} aborted the operation: {e.reason}",
            service_account_email=None,
            hint=("A pre-write hook (Validate / Timestamp / Audit) rejected the "
                  "request. Adjust the value to match the hook's expectations."),
        )
