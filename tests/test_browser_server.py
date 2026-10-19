"""Browser endpoint over the MCP protocol with an in-memory client and a fake driver."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from browser_mcp.browser_server import create_browser_server
from browser_mcp.browsers import BrowserPool
from browser_mcp.config import Settings
from browser_mcp.sessions import SessionManager, SessionManagerConfig
from conftest import FakeDriver


def make_server(pool_size: int = 2, **overrides):
    settings = Settings(_env_file=None, endpoints="browser", **overrides)
    sessions = SessionManager(FakeDriver, BrowserPool(pool_size), SessionManagerConfig(open_wait=0.05))
    return create_browser_server(settings, sessions), sessions


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


async def test_tools_are_listed_without_output_schema():
    mcp, _ = make_server()
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {
        "browser_open",
        "browser_close",
        "browser_list_sessions",
        "browser_navigate",
        "browser_back",
        "browser_tab",
        "browser_click",
        "browser_type",
        "browser_press_keys",
        "browser_select_option",
        "browser_scroll",
        "browser_wait",
        "browser_snapshot",
        "browser_screenshot",
        "browser_read",
        "browser_find",
    }
    assert all(t.output_schema is None for t in tools.values())


async def test_first_action_opens_default_browser_and_returns_state():
    mcp, sessions = make_server()
    async with Client(mcp) as client:
        result = await client.call_tool("browser_navigate", {"url": "https://example.com"})
        again = await client.call_tool("browser_click", {"index": 1})
        assert sessions.count == 1
    assert result.structured_content is None
    body = text_of(result)
    assert body.startswith("OK: navigate ok")
    assert "URL: https://example.com" in body and "[1]<button>" in body
    assert text_of(again).startswith("OK: click ok")
    assert sessions.count == 0  # closed on server shutdown
    assert FakeDriver.instances[0].actions == [
        ("navigate", {"url": "https://example.com", "new_tab": False}),
        ("click", {"index": 1}),
    ]


async def test_failed_action_still_returns_state():
    mcp, _ = make_server()
    async with Client(mcp) as client:
        result = await client.call_tool("browser_click", {"index": 999})
    body = text_of(result)
    assert body.startswith("FAILED: Element index 999")
    assert "URL:" in body


async def test_click_requires_index_or_coordinates():
    mcp, _ = make_server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="index"):
            await client.call_tool("browser_click", {"x": 10})
        await client.call_tool("browser_click", {"x": 10, "y": 20})
    assert FakeDriver.instances[0].actions == [("click", {"coordinate_x": 10, "coordinate_y": 20})]


async def test_type_with_submit_presses_enter():
    mcp, _ = make_server()
    async with Client(mcp) as client:
        result = await client.call_tool("browser_type", {"index": 2, "text": "hello", "submit": True})
    assert "Enter: send_keys ok" in text_of(result)
    assert FakeDriver.instances[0].actions == [
        ("input", {"index": 2, "text": "hello", "clear": True}),
        ("send_keys", {"keys": "Enter"}),
    ]


async def test_snapshot_with_screenshot_returns_image():
    mcp, _ = make_server()
    async with Client(mcp) as client:
        plain = await client.call_tool("browser_snapshot", {})
        with_image = await client.call_tool("browser_snapshot", {"screenshot": True})
    assert [type(c) for c in plain.content] == [TextContent]
    assert [type(c) for c in with_image.content] == [TextContent, ImageContent]
    assert with_image.content[1].mime_type == "image/jpeg"


async def test_read_is_chunked():
    mcp, _ = make_server(browser_read_max_chars=1000)
    async with Client(mcp) as client:
        result = await client.call_tool("browser_read", {"start": 3})
    body = text_of(result)
    assert "Chars 3-58 of 58" in body
    assert "more:" not in body


async def test_open_extra_session_and_close():
    mcp, sessions = make_server()
    async with Client(mcp) as client:
        opened = text_of(await client.call_tool("browser_open", {"url": "https://a.test"}))
        session_id = opened.splitlines()[0].removeprefix("session_id: ")
        listed = text_of(await client.call_tool("browser_list_sessions", {}))
        assert session_id in listed and "yours" in listed
        closed = text_of(await client.call_tool("browser_close", {"session_id": session_id}))
        assert "Closed" in closed
        with pytest.raises(ToolError, match="Unknown browser session"):
            await client.call_tool("browser_snapshot", {"session_id": session_id})
    assert sessions.count == 0


async def test_close_default_session():
    mcp, sessions = make_server()
    async with Client(mcp) as client:
        await client.call_tool("browser_navigate", {"url": "https://a.test"})
        assert "Closed" in text_of(await client.call_tool("browser_close", {}))
        assert "No such" in text_of(await client.call_tool("browser_close", {}))
    assert sessions.count == 0


async def test_busy_pool_is_a_tool_error():
    mcp, _ = make_server(pool_size=1)
    async with Client(mcp) as client:
        await client.call_tool("browser_open", {})
        with pytest.raises(ToolError, match="browsers are busy"):
            await client.call_tool("browser_snapshot", {})


async def test_domain_policy_cannot_be_widened():
    mcp, _ = make_server(browser_allowed_domains="example.com")
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="not permitted"):
            await client.call_tool("browser_open", {"allowed_domains": ["evil.com"]})
        await client.call_tool("browser_open", {"allowed_domains": ["example.com"]})
    assert FakeDriver.instances[-1].allowed_domains == ["example.com"]
