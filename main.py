from __future__ import annotations

import asyncio
import sys

from server.app import build_server
from services.auth_service.client_factory import ClientFactory
from services.auth_service.credentials import load_credentials
from services.dispatch_service.db import close_pool, get_pool
from services.dispatch_service.manager import DispatchManager
from services.dispatch_service.repository import DispatchRepository
from services.sheets_service.hook_registry import HookHub
from services.sheets_service.hooks.registration import register_default_hooks
from services.sheets_service.manager import SheetsManager
from shared.config_loader import load_config
from shared.exceptions import GoogleAuthError
from shared.logger import get_logger

log = get_logger("main")


async def self_test(sheets_mgr: SheetsManager, spreadsheet_id: str | None) -> None:
    """One cheap call to confirm credentials work before MCP handshake.

    NOTE (I21): this only verifies READ permission — the SA must additionally
    have EDITOR access for any write tool to succeed. The first write op will
    surface a 403 if Editor is missing; we log a clear hint here so operators
    know to expect that.
    """
    try:
        if spreadsheet_id:
            from services.sheets_service.models import ListTabsRequest
            tabs = await sheets_mgr.list_tabs(ListTabsRequest(spreadsheet_id=spreadsheet_id))
            log.info("self-test: sheets.list_tabs on %s OK (%d tabs)",
                     spreadsheet_id, len(tabs.tabs))
            log.info("self-test: READ confirmed. Editor access required for writes — "
                     "share the sheet with the SA email if writes 403 later.")
        else:
            log.warning("self-test: SPREADSHEET_ID not configured; skipping live read. "
                        "Dispatch tools will require spreadsheet_id per call.")
    except Exception as e:
        log.error("self-test failed (%s): %s", type(e).__name__, str(e)[:200])
        raise GoogleAuthError(f"credential self-test failed: {e}") from e


async def main() -> None:
    config = load_config()
    creds = load_credentials(config)
    factory = ClientFactory(creds)
    sa_email = getattr(creds, "service_account_email", "<unknown>")
    log.info("authenticated as %s", sa_email)

    hub = HookHub()
    register_default_hooks(hub, config)

    sheets_mgr = SheetsManager(factory, hub, config.sheets)

    # Optional DB repository — None when DATABASE_URL is empty (mirror disabled).
    pool = await get_pool()
    matcher = None
    repo = None
    if pool is not None:
        from services.dispatch_service.sku_matcher import SkuMatcher
        matcher = SkuMatcher(pool=pool, threshold=80.0)
        sku_count = await matcher.load()
        log.info("SkuMatcher loaded %d SKUs from all_sku", sku_count)
        repo = DispatchRepository(pool=pool, sku_matcher=matcher)
        log.info("DB mirror enabled — Postgres pool initialised")
    else:
        log.info("DB mirror disabled (DATABASE_URL not set)")

    dispatch_mgr = DispatchManager(sheets_mgr, factory, config.spreadsheet_id, repo=repo)

    if config.spreadsheet_id:
        log.info("configured SPREADSHEET_ID: %s", config.spreadsheet_id)
    else:
        log.warning("SPREADSHEET_ID not set in env; dispatch tools will require it per-call")

    await self_test(sheets_mgr, config.spreadsheet_id)

    server = build_server(sheets_mgr, dispatch_mgr)

    log.info("starting MCP server over stdio")
    try:
        await server.run_stdio_async()
    finally:
        await close_pool()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log.error("fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    run()
