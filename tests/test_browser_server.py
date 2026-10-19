"""Browser endpoint over the MCP protocol with an in-memory client and a fake driver."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from browser_mcp.browser_server import create_browser_server
from browser_mcp.config import Settings
from browser_mcp.sessions import BrowserSlot, SlotConfig
from conftest import FakeDriver


def make_server(**overrides):
    settings = Settings(_env_file=None, **overrides)
    slot = BrowserSlot(FakeDriver, SlotConfig(busy_wait=0.05))
    return create_browser_server(settings, slot), slot


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


async def test_tools_are_listed_without_output_schema():
    mcp, _ = make_server()
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {
        "browser_close",
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


async def test_first_action_opens_the_browser_and_returns_state():
    mcp, slot = make_server()
    async with Client(mcp) as client:
        result = await client.call_tool("browser_navigate", {"url": "https://example.com"})
        again = await client.call_tool("browser_click", {"index": 1})
        assert slot.status()["state"] == "idle"
    assert result.structured_content is None
    body = text_of(result)
    assert body.startswith("NOTE: a new browser was started for you")
    assert "OK: navigate ok" in body
    assert "URL: https://example.com" in body and "[1]<button>" in body
    assert text_of(again).startswith("OK: click ok")  # the notice comes once
    assert slot.status()["state"] == "free"  # closed on server shutdown
    assert FakeDriver.instances[0].closed
    assert FakeDriver.instances[0].actions == [
        ("navigate", {"url": "https://example.com", "new_tab": False}),
        ("click", {"index": 1}),
    ]


async def test_failed_action_still_returns_state():
    mcp, _ = make_server()
    async with Client(mcp) as client:
        await client.call_tool("browser_snapshot", {})
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
    for result in (plain, with_image):  # real text, not a JSON-encoded list of strings
        assert "\nPage:\n[1]<button>" in result.content[0].text
    assert with_image.content[1].mime_type == "image/jpeg"


async def test_read_is_chunked():
    mcp, _ = make_server(browser_read_max_chars=1000)
    async with Client(mcp) as client:
        await client.call_tool("browser_snapshot", {})
        result = await client.call_tool("browser_read", {"start": 3})
    body = text_of(result)
    assert body.startswith("URL: ")
    assert "Chars 3-58 of 58" in body
    assert "more:" not in body


async def test_close_frees_the_browser():
    mcp, slot = make_server()
    async with Client(mcp) as client:
        await client.call_tool("browser_navigate", {"url": "https://a.test"})
        assert "Browser closed" in text_of(await client.call_tool("browser_close", {}))
        assert slot.status()["state"] == "free"
        assert "no open browser" in text_of(await client.call_tool("browser_close", {}))
        again = text_of(await client.call_tool("browser_snapshot", {}))
    assert again.startswith("NOTE: a new browser was started")
    assert len(FakeDriver.instances) == 2 and FakeDriver.instances[0].closed


async def test_instructions_mention_the_handover_limit():
    mcp, _ = make_server(browser_handover_idle_seconds=45)
    assert "inactive for 45s" in mcp.instructions
