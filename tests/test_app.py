"""The assembled ASGI app: endpoint routing, health/ready, per-endpoint auth, shared pool."""

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from browser_mcp.app import build_components, create_app
from browser_mcp.config import Settings
from conftest import FakeDriver, FakeRunner

BASE = "http://testserver"


def components(**overrides):
    base = {"llm_base_url": "http://127.0.0.1:9", "wait_default_seconds": 5}
    settings = Settings(_env_file=None, **{**base, **overrides})
    return build_components(settings, runner_factory=lambda pool: FakeRunner(), driver_factory=FakeDriver)


class _Lifespan:
    """Runs the ASGI lifespan around a test, like uvicorn does."""

    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        import asyncio

        self._queue: asyncio.Queue = asyncio.Queue()
        self._started = asyncio.Event()
        self._done = asyncio.Event()

        async def receive():
            return await self._queue.get()

        self._failure: str | None = None

        async def send(message):
            if message["type"].endswith(".failed"):
                self._failure = message.get("message")
                self._started.set()
                self._done.set()
            elif message["type"] == "lifespan.startup.complete":
                self._started.set()
            elif message["type"] == "lifespan.shutdown.complete":
                self._done.set()

        scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
        self._task = asyncio.create_task(self.app(scope, receive, send))
        await self._queue.put({"type": "lifespan.startup"})
        await asyncio.wait_for(self._started.wait(), 5)
        if self._failure:
            raise RuntimeError(self._failure)
        return self.app

    async def __aexit__(self, *exc):
        import asyncio

        await self._queue.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(self._done.wait(), 10)
        await self._task
        if self._failure:
            raise RuntimeError(self._failure)


def http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


def mcp_client(app, path: str, token: str | None = None) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    transport = StreamableHttpTransport(
        f"{BASE}{path}",
        headers=headers,
        httpx_client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kw),
    )
    return Client(transport)


async def test_health_lists_enabled_endpoints():
    c = components()
    app = create_app(c)
    async with _Lifespan(app), http(app) as client:
        body = (await client.get("/health")).json()
    assert body["endpoints"] == {"browser": "/mcp/browser", "agent": "/mcp/agent"}
    assert body["browsers"] == {"in_use": 0, "max": 2}
    assert body["browser_sessions"] == 0 and body["active_tasks"] == 0


async def test_ready_without_agent_needs_no_llm():
    app = create_app(components(endpoints="browser"))
    async with _Lifespan(app), http(app) as client:
        response = await client.get("/ready")
        health = (await client.get("/health")).json()
    assert response.status_code == 200
    assert response.json()["llm"] == "not used"
    assert "active_tasks" not in health


async def test_ready_reports_unreachable_llm():
    app = create_app(components())
    async with _Lifespan(app), http(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 503


async def test_each_endpoint_serves_its_own_tools():
    app = create_app(components())
    async with _Lifespan(app):
        async with mcp_client(app, "/mcp/browser") as client:
            browser_tools = {t.name for t in await client.list_tools()}
            await client.call_tool("browser_navigate", {"url": "https://example.com"})
            await client.call_tool("browser_click", {"index": 1})
        async with mcp_client(app, "/mcp/agent") as client:
            agent_tools = {t.name for t in await client.list_tools()}
            result = await client.call_tool("browser_run_task", {"task": "open example.com"})
    assert "browser_click" in browser_tools and "browser_run_task" not in browser_tools
    assert agent_tools == {
        "browser_run_task",
        "browser_wait_task",
        "browser_get_task",
        "browser_cancel_task",
        "browser_list_tasks",
    }
    assert result.structured_content["status"] == "succeeded"
    assert len(FakeDriver.instances) == 1
    assert [a for a, _ in FakeDriver.instances[0].actions] == ["navigate", "click"]
    assert FakeDriver.instances[0].closed  # lifespan shutdown closed the browser


async def test_stateless_clients_share_default_browser_but_can_isolate():
    app = create_app(components(endpoints="browser"))
    async with _Lifespan(app):
        async with mcp_client(app, "/mcp/browser") as first:
            await first.call_tool("browser_navigate", {"url": "https://a.test"})
            opened = await first.call_tool("browser_open", {})
        async with mcp_client(app, "/mcp/browser") as second:
            await second.call_tool("browser_click", {"index": 1})
    shared, isolated = FakeDriver.instances
    assert [a for a, _ in shared.actions] == ["navigate", "click"]
    assert isolated.actions == []
    assert opened.content[0].text.startswith("session_id: ")


async def _legacy_call(client: httpx.AsyncClient, session_id: str | None, method: str, params: dict):
    """One JSON-RPC request the way 2025-protocol clients send it (stateful, Mcp-Session-Id)."""
    headers = {"Accept": "application/json, text/event-stream"}
    if session_id:
        headers |= {"Mcp-Session-Id": session_id, "MCP-Protocol-Version": "2025-06-18"}
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if not method.startswith("notifications/"):
        body["id"] = 1
    response = await client.post("/mcp/browser", json=body, headers=headers)
    assert response.status_code < 300, response.text
    return response


async def test_legacy_sessions_get_their_own_default_browser():
    app = create_app(components(endpoints="browser"))
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    async with _Lifespan(app), http(app) as client:
        session_ids = []
        for _ in range(2):
            response = await _legacy_call(client, None, "initialize", init)
            session_id = response.headers["mcp-session-id"]
            await _legacy_call(client, session_id, "notifications/initialized", {})
            session_ids.append(session_id)
        for session_id in [*session_ids, session_ids[0]]:
            call = {"name": "browser_navigate", "arguments": {"url": f"https://{session_id[:6]}.test"}}
            await _legacy_call(client, session_id, "tools/call", call)
    first, second = FakeDriver.instances
    assert len(first.actions) == 2 and len(second.actions) == 1


async def test_disabled_endpoint_is_not_found():
    app = create_app(components(endpoints="browser"))
    async with _Lifespan(app), http(app) as client:
        response = await client.post("/mcp/agent", json={})
    assert response.status_code == 404


async def test_per_endpoint_tokens():
    browser_token, agent_token = "b" * 20, "a" * 20
    app = create_app(components(browser_auth_token=browser_token, agent_auth_token=agent_token))
    async with _Lifespan(app):
        async with mcp_client(app, "/mcp/browser", browser_token) as client:
            assert await client.list_tools()
        with pytest.raises(Exception):  # noqa: B017 - the client surfaces 401 as various errors
            async with mcp_client(app, "/mcp/agent", browser_token) as client:
                await client.list_tools()
        async with mcp_client(app, "/mcp/agent", agent_token) as client:
            assert await client.list_tools()
