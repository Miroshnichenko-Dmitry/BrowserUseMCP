"""The assembled ASGI app: routing, /health, auth, telling agents apart over HTTP."""

import asyncio

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from browser_mcp.app import build_components, create_app
from browser_mcp.config import Settings
from conftest import FakeDriver

BASE = "http://testserver"
PROTOCOL = "2025-06-18"  # handshake protocol: clients get an Mcp-Session-Id


def components(**overrides):
    settings = Settings(_env_file=None, **overrides)
    return build_components(settings, driver_factory=FakeDriver)


class _Lifespan:
    """Runs the ASGI lifespan around a test, like uvicorn does."""

    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._started = asyncio.Event()
        self._done = asyncio.Event()
        self._failure: str | None = None

        async def receive():
            return await self._queue.get()

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
        await self._queue.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(self._done.wait(), 10)
        await self._task
        if self._failure:
            raise RuntimeError(self._failure)


def http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


def mcp_client(app, token: str | None = None) -> Client:
    """A current (stateless protocol) MCP client."""
    headers = {"Authorization": f"Bearer {token}"} if token else None
    transport = StreamableHttpTransport(
        f"{BASE}/mcp/browser",
        headers=headers,
        httpx_client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kw),
    )
    return Client(transport)


class HandshakeAgent:
    """An agent on the 2025 handshake protocol: one Mcp-Session-Id per connection."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers |= {"Mcp-Session-Id": self.session_id, "MCP-Protocol-Version": PROTOCOL}
        return headers

    async def _post(self, method: str, params: dict, request_id: int | None = 1) -> httpx.Response:
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        response = await self.client.post("/mcp/browser", json=body, headers=self._headers())
        assert response.status_code < 300, response.text
        return response

    async def connect(self) -> "HandshakeAgent":
        init = {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
        response = await self._post("initialize", init)
        self.session_id = response.headers["mcp-session-id"]
        await self._post("notifications/initialized", {}, request_id=None)
        return self

    async def call(self, tool: str, arguments: dict) -> str:
        """Returns the response body: tool text or the error, JSON-escaped."""
        response = await self._post("tools/call", {"name": tool, "arguments": arguments})
        return response.text

    async def disconnect(self) -> int:
        response = await self.client.delete("/mcp/browser", headers=self._headers())
        return response.status_code


async def test_health_reports_a_free_browser():
    app = create_app(components())
    async with _Lifespan(app), http(app) as client:
        body = (await client.get("/health")).json()
    assert body["endpoint"] == "/mcp/browser"
    assert body["browser"] == {"state": "free", "waiting": 0}


async def test_only_browser_tools_are_served():
    app = create_app(components())
    async with _Lifespan(app), mcp_client(app) as client:
        tools = {t.name for t in await client.list_tools()}
        await client.call_tool("browser_navigate", {"url": "https://example.com"})
        await client.call_tool("browser_click", {"index": 1})
    assert "browser_click" in tools and "browser_close" in tools
    assert not {"browser_open", "browser_run_task", "browser_list_sessions"} & tools
    assert [a for a, _ in FakeDriver.instances[0].actions] == ["navigate", "click"]
    assert FakeDriver.instances[0].closed  # lifespan shutdown closed the browser


async def test_agent_endpoint_is_gone():
    app = create_app(components())
    async with _Lifespan(app), http(app) as client:
        assert (await client.post("/mcp/agent", json={})).status_code == 404


async def test_second_agent_is_busy_while_first_is_active():
    app = create_app(components(browser_handover_idle_seconds=30, browser_busy_wait_seconds=0.05))
    async with _Lifespan(app), http(app) as client:
        first, second = HandshakeAgent(client), HandshakeAgent(client)
        await first.connect()
        await second.connect()
        assert "OK: navigate ok" in await first.call("browser_navigate", {"url": "https://a.test"})
        busy = await second.call("browser_navigate", {"url": "https://b.test"})
        health = (await client.get("/health")).json()
    assert '"isError":true' in busy and "in use by another agent" in busy
    assert len(FakeDriver.instances) == 1
    assert health["browser"]["owner"].startswith("mcp-session ")
    assert first.session_id not in str(health)  # full session ids are not exposed


async def test_second_agent_takes_over_after_first_idles():
    app = create_app(components(browser_handover_idle_seconds=0.1, browser_busy_wait_seconds=2))
    async with _Lifespan(app), http(app) as client:
        first, second = HandshakeAgent(client), HandshakeAgent(client)
        await first.connect()
        await second.connect()
        await first.call("browser_navigate", {"url": "https://a.test"})
        taken = await second.call("browser_navigate", {"url": "https://b.test"})
        back = await first.call("browser_snapshot", {})  # waits for second to idle, then takes over
        states = [(d.closed, d.last_url) for d in FakeDriver.instances]
    assert "OK: navigate ok" in taken and "https://b.test" in taken
    assert "previous browser was closed: another agent was waiting" in back
    assert "URL: about:blank" in back  # a fresh browser, not the first agent's old page
    assert states == [(True, "https://a.test"), (True, "https://b.test"), (False, None)]


async def test_disconnect_frees_the_browser_at_once():
    app = create_app(components(browser_handover_idle_seconds=30, browser_busy_wait_seconds=0.05))
    async with _Lifespan(app), http(app) as client:
        first, second = HandshakeAgent(client), HandshakeAgent(client)
        await first.connect()
        await second.connect()
        await first.call("browser_navigate", {"url": "https://a.test"})
        assert await first.disconnect() < 300
        assert (await client.get("/health")).json()["browser"]["state"] == "free"
        taken = await second.call("browser_navigate", {"url": "https://b.test"})
    assert "OK: navigate ok" in taken
    assert FakeDriver.instances[0].closed


async def test_agent_tokens_tell_stateless_agents_apart():
    alpha, beta = "a" * 20, "b" * 20
    app = create_app(
        components(
            auth_tokens=f"alpha:{alpha},beta:{beta}",
            browser_handover_idle_seconds=0.1,
            browser_busy_wait_seconds=2,
        )
    )
    async with _Lifespan(app), http(app) as client:
        async with mcp_client(app, alpha) as first:
            await first.call_tool("browser_navigate", {"url": "https://a.test"})
            assert (await client.get("/health")).json()["browser"]["owner"] == "alpha"
        async with mcp_client(app, alpha) as reconnected:  # same agent, new connection: same browser
            await reconnected.call_tool("browser_click", {"index": 1})
        async with mcp_client(app, beta) as second:
            await second.call_tool("browser_navigate", {"url": "https://b.test"})
            assert (await client.get("/health")).json()["browser"]["owner"] == "beta"
    first_browser, _ = FakeDriver.instances
    assert [a for a, _ in first_browser.actions] == ["navigate", "click"]
    assert first_browser.closed


async def test_stateless_agents_with_a_shared_token_count_as_one():
    token = "s" * 20
    app = create_app(components(auth_token=token, browser_busy_wait_seconds=0.05))
    async with _Lifespan(app):
        async with mcp_client(app, token) as first:
            await first.call_tool("browser_navigate", {"url": "https://a.test"})
        async with mcp_client(app, token) as second:
            await second.call_tool("browser_click", {"index": 1})
    assert len(FakeDriver.instances) == 1


async def test_tokens_are_enforced():
    app = create_app(components(auth_tokens=f"alpha:{'a' * 20}"))
    async with _Lifespan(app):
        with pytest.raises(Exception):  # noqa: B017 - the client surfaces 401 as various errors
            async with mcp_client(app, "w" * 20) as client:
                await client.list_tools()
        async with http(app) as client:
            response = await client.delete("/mcp/browser", headers={"Mcp-Session-Id": "x"})
        assert response.status_code == 401
        async with mcp_client(app, "a" * 20) as client:
            assert await client.list_tools()
