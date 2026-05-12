from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from server.registry import register_all
from services.dispatch_service.manager import DispatchManager
from services.sheets_service.manager import SheetsManager


def build_server(sheets_mgr: SheetsManager, dispatch_mgr: DispatchManager) -> FastMCP:
    server = FastMCP("dispatch-mcp-server")
    register_all(server, sheets_mgr, dispatch_mgr)
    return server
