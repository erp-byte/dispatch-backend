from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from services.dispatch_service.manager import DispatchManager
from services.dispatch_service.tools import register as register_dispatch_tools
from services.sheets_service.manager import SheetsManager
from services.sheets_service.tools import register as register_sheets_tools


def register_all(server: FastMCP, sheets_mgr: SheetsManager,
                 dispatch_mgr: DispatchManager) -> None:
    register_sheets_tools(server, sheets_mgr)
    register_dispatch_tools(server, dispatch_mgr)
