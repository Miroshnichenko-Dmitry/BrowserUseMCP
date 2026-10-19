"""Assembles the enabled endpoints into one ASGI app with shared browser resources.

/mcp/browser   low-level browser tools  (SessionManager)
/mcp/agent     browser-use agent tasks  (TaskManager)
/health        liveness
/ready         readiness (LLM reachable, if the agent endpoint is enabled)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from fastmcp import FastMCP
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .agent_server import create_agent_server
from .browser_server import create_browser_server
from .browsers import BrowserPool
from .config import ENDPOINT_PATHS, Endpoint, Settings
from .llm import check_llm_ready
from .sessions import DriverFactory, SessionManager, SessionManagerConfig
from .tasks import Runner, TaskManager

logger = logging.getLogger(__name__)


@dataclass
class Components:
    """Everything the endpoints share; built once per process."""

    settings: Settings
    pool: BrowserPool
    servers: dict[Endpoint, FastMCP]
    sessions: SessionManager | None = None
    tasks: TaskManager | None = None


def build_components(
    settings: Settings,
    runner_factory: Callable[[BrowserPool], Runner] | None = None,
    driver_factory: DriverFactory | None = None,
) -> Components:
    """Factories are injectable so tests can run without a browser or an LLM."""
    pool = BrowserPool(settings.max_browsers)
    components = Components(settings=settings, pool=pool, servers={})

    if settings.browser_enabled:
        if driver_factory is None:
            from .driver import BrowserUseDriver

            def driver_factory(session_id: str, domains: list[str] | None) -> BrowserUseDriver:
                return BrowserUseDriver(settings, session_id, domains)

        components.sessions = SessionManager(
            driver_factory,
            pool,
            SessionManagerConfig(
                idle_timeout=settings.browser_session_idle_seconds,
                max_per_owner=settings.browser_max_sessions_per_client,
                open_wait=settings.browser_open_wait_seconds,
            ),
        )
        components.servers["browser"] = create_browser_server(
            settings, components.sessions, auth=_auth(settings, "browser")
        )

    if settings.agent_enabled:
        if runner_factory is None:
            from .runner import BrowserAgentRunner

            def runner_factory(p: BrowserPool) -> Runner:
                return BrowserAgentRunner(settings, p)

        server, components.tasks = create_agent_server(
            settings, runner_factory(pool), auth=_auth(settings, "agent")
        )
        components.servers["agent"] = server

    return components


def create_app(components: Components) -> ASGIApp:
    settings = components.settings
    mcp_apps = {
        ENDPOINT_PATHS[name]: server.http_app(path=ENDPOINT_PATHS[name])
        for name, server in components.servers.items()
    }

    async def health(_: Request) -> JSONResponse:
        """Liveness: the process is up and serving."""
        body: dict[str, object] = {
            "status": "ok",
            "version": __version__,
            "endpoints": {name: ENDPOINT_PATHS[name] for name in components.servers},
            "browsers": {"in_use": components.pool.in_use, "max": components.pool.size},
        }
        if components.sessions is not None:
            body["browser_sessions"] = components.sessions.count
        if components.tasks is not None:
            body["active_tasks"] = components.tasks.active_count
        return JSONResponse(body)

    async def ready(_: Request) -> JSONResponse:
        """Readiness: the agent's LLM backend is reachable (browser endpoint needs no LLM)."""
        if not settings.agent_enabled:
            return JSONResponse({"status": "ready", "llm": "not used"})
        ok, detail = await check_llm_ready(settings)
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "llm": detail}, status_code=200 if ok else 503
        )

    # Each MCP app keeps its own routes, auth middleware and session manager; the dispatcher only
    # picks one by path prefix. The fallback app owns /health, /ready and every MCP app's lifespan.
    fallback = Starlette(
        routes=[Route("/health", health, methods=["GET"]), Route("/ready", ready, methods=["GET"])],
        lifespan=combine_lifespans(*(app.lifespan for app in mcp_apps.values())),
    )
    return PathDispatcher(mcp_apps, fallback)


class PathDispatcher:
    """Routes `/mcp/<name>` (and its OAuth well-known metadata) to the matching MCP app, unchanged.

    Unlike Starlette's Mount, the path is not rewritten, so `/mcp/browser` works without a
    trailing-slash redirect, which some MCP clients do not follow on POST.
    """

    def __init__(self, apps: dict[str, ASGIApp], fallback: ASGIApp):
        self._apps = apps
        self._fallback = fallback

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path: str = scope["path"]
            for prefix, app in self._apps.items():
                if (
                    path == prefix
                    or path.startswith(prefix + "/")
                    or (path.startswith("/.well-known/") and path.endswith(prefix))
                ):
                    await app(scope, receive, send)
                    return
        await self._fallback(scope, receive, send)


def _auth(settings: Settings, endpoint: Endpoint):
    token = settings.token_for(endpoint)
    if token is None:
        return None
    from .auth import BearerTokenVerifier

    return BearerTokenVerifier(token.get_secret_value())
