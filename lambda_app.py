"""AWS Lambda entry point — exposes the dispatch MCP server over HTTP.

Architecture:
    Lambda Function URL  -->  Mangum (ASGI adapter)  -->  Starlette app
                                                          -> BearerTokenAuthMiddleware
                                                          -> _DbWarmupMiddleware
                                                          -> FastMCP.streamable_http_app()

Key differences from `main.py` (stdio mode):
  * Transport is MCP "streamable HTTP" instead of stdio.
  * FastMCP is built with ``stateless_http=True`` and ``json_response=True``
    so each request is self-contained (no session state on the server) and
    the response is plain JSON instead of SSE. This matches the BUFFERED
    invocation mode of a Lambda Function URL.
  * Self-test on startup is intentionally skipped — it would slow down
    every cold start. Credential failures still surface as 5xx on the
    first real tool call.
  * A bearer-token middleware gates the endpoint when the
    ``MCP_AUTH_TOKEN`` env var is set. Leave it unset only for local
    testing; never expose a public Function URL without it.
  * DB mirror (Postgres pool + SkuMatcher + DispatchRepository) is wired
    in lazily on the FIRST request, NOT at module load — there is no
    asyncio event loop available at module initialisation time in Lambda.
    The ``_DbWarmupMiddleware`` handles this transparently; all tool
    closures capture the same ``dispatch_mgr`` instance and see the
    updated ``._repo`` after the warmup completes.

Required env vars (set in the Lambda config):
    GOOGLE_CREDENTIALS_JSON   service-account JSON on a single line
    SPREADSHEET_ID            default dispatch sheet (optional)
    MCP_AUTH_TOKEN            shared secret for Authorization header (optional)
    DATABASE_URL              Postgres connection string (optional — enables DB mirror)
    LOG_LEVEL                 INFO | DEBUG | WARNING | ERROR
"""

from __future__ import annotations

import asyncio
import os

from mangum import Mangum
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from server.registry import register_all
from services.auth_service.client_factory import ClientFactory
from services.auth_service.credentials import load_credentials
from services.dispatch_service.db import get_pool
from services.dispatch_service.manager import DispatchManager
from services.dispatch_service.repository import DispatchRepository
from services.dispatch_service.sku_matcher import SkuMatcher
from services.sheets_service.hook_registry import HookHub
from services.sheets_service.hooks.registration import register_default_hooks
from services.sheets_service.manager import SheetsManager
from shared.config_loader import load_config
from shared.logger import get_logger

log = get_logger("lambda")


class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests whose ``Authorization`` header does not match the
    configured ``MCP_AUTH_TOKEN``. If the env var is empty the middleware
    is a no-op — convenient for local Docker testing only."""

    def __init__(self, app, expected_token: str | None):
        super().__init__(app)
        self._expected = expected_token

    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not self._expected:
            return await call_next(request)
        got = request.headers.get("authorization", "")
        if got != f"Bearer {self._expected}":
            return JSONResponse(
                {"error": "unauthorized",
                 "hint": "send 'Authorization: Bearer <MCP_AUTH_TOKEN>'"},
                status_code=401,
            )
        return await call_next(request)


class _DbWarmupMiddleware(BaseHTTPMiddleware):
    """On the first request, initialise the DB mirror and attach it to
    the long-lived ``dispatch_mgr`` instance.

    Why lazy? Lambda has no running event loop at module load time — we
    cannot ``await get_pool()`` during cold-start. Mangum starts the event
    loop per-request, so the earliest safe place to run async DB init is
    inside a middleware ``dispatch()`` call.

    Thread/concurrency safety: asyncio.Lock() serialises concurrent
    first-requests on the same warm instance; the ``_warmed`` flag short-
    circuits subsequent requests with zero overhead.
    """

    def __init__(self, app, dispatch_mgr: DispatchManager):
        super().__init__(app)
        self._dispatch_mgr = dispatch_mgr
        self._warmed = False
        self._lock = asyncio.Lock()

    async def dispatch(self, request, call_next):
        if not self._warmed:
            async with self._lock:
                if not self._warmed:
                    pool = await get_pool()
                    if pool is not None:
                        matcher = SkuMatcher(pool=pool, threshold=80.0)
                        sku_count = await matcher.load()
                        log.info("SkuMatcher loaded %d SKUs from all_sku", sku_count)
                        self._dispatch_mgr._repo = DispatchRepository(
                            pool=pool, sku_matcher=matcher,
                        )
                        log.info("DB mirror enabled — Postgres pool initialised")
                    else:
                        log.info("DB mirror disabled (DATABASE_URL not set)")
                    self._warmed = True
        return await call_next(request)


def _make_health_route(handler):
    """Build a Starlette Route for GET /health."""
    from starlette.routing import Route
    return Route("/health", handler, methods=["GET"])


def _build_asgi_app():
    config = load_config()
    creds = load_credentials(config)
    factory = ClientFactory(creds)

    sa_email = getattr(creds, "service_account_email", "<unknown>")
    log.info("authenticated as %s", sa_email)

    hub = HookHub()
    register_default_hooks(hub, config)
    sheets_mgr = SheetsManager(factory, hub, config.sheets)
    # repo=None — the DB mirror is wired in lazily by _DbWarmupMiddleware on
    # the first request. _repo_call() is a no-op when _repo is None, so all
    # sheet-only operations work correctly before the warmup fires.
    dispatch_mgr = DispatchManager(sheets_mgr, factory, config.spreadsheet_id)

    # Stateless HTTP + JSON response = each request stands alone. No SSE.
    # That maps cleanly onto a Lambda Function URL in BUFFERED mode.
    # transport_security disables the SDK's DNS-rebinding host check, which
    # is intended for localhost MCP servers. We're HTTPS-fronted behind
    # Render/API Gateway, so the rebinding threat model doesn't apply and
    # the default would reject our public hostname.
    mcp_server = FastMCP(
        "dispatch-mcp-server",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    register_all(mcp_server, sheets_mgr, dispatch_mgr)

    asgi = mcp_server.streamable_http_app()

    # Health endpoint for the EventBridge warmer. Runs through _DbWarmupMiddleware
    # so the asyncpg pool + SkuMatcher stay loaded across the warm-pool lifetime.
    async def _health(request):
        return JSONResponse({
            "ok": True,
            "db_mirror_enabled": dispatch_mgr._repo is not None,
            "sku_count": (
                dispatch_mgr._repo._sku_matcher.loaded_count
                if dispatch_mgr._repo is not None
                and dispatch_mgr._repo._sku_matcher is not None
                else 0
            ),
        })
    asgi.router.routes.insert(0, _make_health_route(_health))

    # Middleware stack (innermost → outermost — Starlette wraps in reverse):
    #   1. _DbWarmupMiddleware  — fires once, before any tool sees the request
    #   2. BearerTokenAuthMiddleware — outermost; rejects unauthenticated calls
    #      before any work is done. /health is exempted from auth so the
    #      EventBridge warmer can ping it without secrets.
    asgi.add_middleware(_DbWarmupMiddleware, dispatch_mgr=dispatch_mgr)
    asgi.add_middleware(
        BearerTokenAuthMiddleware,
        expected_token=os.environ.get("MCP_AUTH_TOKEN") or None,
    )
    return asgi


# Built once per cold start; reused across warm invocations.
_app = _build_asgi_app()

# Lifespan must be "off" because Mangum's default lifespan="auto" probes for
# Starlette lifespan handlers, which the FastMCP-generated app does not run
# the same way under Lambda. The MCP HTTP transport is request/response, so
# we don't need a lifespan loop here.
handler = Mangum(_app, lifespan="off", api_gateway_base_path="/")
