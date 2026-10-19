"""Assembles the browser endpoint into an ASGI app.

/mcp/browser   low-level browser tools, one agent at a time (BrowserSlot)
/health        liveness and who holds the browser
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .browser_server import ANONYMOUS, create_browser_server, session_owner
from .config import MCP_PATH, Settings
from .sessions import BrowserSlot, DriverFactory, SlotConfig

logger = logging.getLogger(__name__)


@dataclass
class Components:
    """Everything the server consists of; built once per process."""

    settings: Settings
    slot: BrowserSlot
    server: FastMCP


def build_components(settings: Settings, driver_factory: DriverFactory | None = None) -> Components:
    """The driver factory is injectable so tests can run without a browser."""
    if driver_factory is None:
        from .driver import BrowserUseDriver

        def driver_factory(session_id: str) -> BrowserUseDriver:
            return BrowserUseDriver(settings, session_id)

    slot = BrowserSlot(
        driver_factory,
        SlotConfig(
            handover_idle=settings.browser_handover_idle_seconds,
            busy_wait=settings.browser_busy_wait_seconds,
            max_age=settings.browser_session_max_seconds,
        ),
    )
    server = create_browser_server(settings, slot, auth=_auth(settings))
    return Components(settings=settings, slot=slot, server=server)


def create_app(components: Components) -> ASGIApp:
    slot = components.slot
    mcp_app = components.server.http_app(path=MCP_PATH)

    async def health(_: Request) -> JSONResponse:
        """Liveness: the process is up and serving."""
        browser = slot.status()
        if "owner" in browser:
            browser["owner"] = _public_owner(str(browser["owner"]))
        return JSONResponse(
            {"status": "ok", "version": __version__, "endpoint": MCP_PATH, "browser": browser}
        )

    async def on_session_end(mcp_session_id: str) -> None:
        if await slot.release(session_owner(mcp_session_id), "MCP session ended"):
            logger.info("client disconnected, browser freed")

    # The MCP app keeps its own routes, auth middleware and session manager; the dispatcher only
    # picks it by path. The fallback app owns /health and the MCP app's lifespan.
    fallback = Starlette(routes=[Route("/health", health, methods=["GET"])], lifespan=mcp_app.lifespan)
    return PathDispatcher(MCP_PATH, mcp_app, fallback, on_session_end)


class PathDispatcher:
    """Routes `/mcp/browser` (and its OAuth well-known metadata) to the MCP app, unchanged.

    Unlike Starlette's Mount, the path is not rewritten, so `/mcp/browser` works without a
    trailing-slash redirect, which some MCP clients do not follow on POST. A successful DELETE
    (the client ending its MCP session) frees that client's browser right away.
    """

    def __init__(
        self,
        prefix: str,
        app: ASGIApp,
        fallback: ASGIApp,
        on_session_end: Callable[[str], Awaitable[None]] | None = None,
    ):
        self._prefix = prefix
        self._app = app
        self._fallback = fallback
        self._on_session_end = on_session_end

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and self._matches(scope["path"]):
            if scope["type"] == "http" and scope["method"] == "DELETE" and self._on_session_end:
                await self._delete(scope, receive, send)
            else:
                await self._app(scope, receive, send)
            return
        await self._fallback(scope, receive, send)

    def _matches(self, path: str) -> bool:
        prefix = self._prefix
        return (
            path == prefix
            or path.startswith(prefix + "/")
            or (path.startswith("/.well-known/") and path.endswith(prefix))
        )

    async def _delete(self, scope: Scope, receive: Receive, send: Send) -> None:
        status = 0

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self._app(scope, receive, capture)
        session_id = Headers(scope=scope).get("mcp-session-id")
        if session_id and 200 <= status < 300 and self._on_session_end is not None:  # 401: not theirs
            await self._on_session_end(session_id)


def _public_owner(owner: str) -> str:
    """Never expose a full MCP session id on the unauthenticated /health."""
    if owner.startswith("session:"):
        return f"mcp-session {owner.removeprefix('session:')[:8]}…"
    if owner.startswith("agent:"):
        return owner.removeprefix("agent:")
    return ANONYMOUS


def _auth(settings: Settings):
    tokens = settings.token_map
    if not tokens:
        return None
    from .auth import BearerTokenVerifier

    return BearerTokenVerifier(tokens)
