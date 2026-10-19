"""Agent endpoint over the MCP protocol with an in-memory client and a fake agent."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from browser_mcp.agent_server import create_agent_server
from browser_mcp.config import Settings
from conftest import FakeRunner


def settings(**overrides) -> Settings:
    base = {"wait_default_seconds": 5, "wait_max_seconds": 10, "llm_base_url": "http://127.0.0.1:9"}
    return Settings(_env_file=None, **{**base, **overrides})


async def test_tools_are_listed():
    mcp, _ = create_agent_server(settings(), FakeRunner())
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {
        "browser_run_task",
        "browser_wait_task",
        "browser_get_task",
        "browser_cancel_task",
        "browser_list_tasks",
    }


async def test_run_task_returns_final_result_when_fast():
    mcp, _ = create_agent_server(settings(), FakeRunner(steps=2))
    progress: list[float] = []

    async def on_progress(value, total, message):
        progress.append(value)

    async with Client(mcp, progress_handler=on_progress) as client:
        result = await client.call_tool("browser_run_task", {"task": "open example.com"})
    data = result.structured_content
    assert data["done"] is True
    assert data["status"] == "succeeded"
    assert data["result"] == "ok"


async def test_long_task_requires_wait():
    mcp, _ = create_agent_server(settings(), FakeRunner(steps=40, step_delay=0.02))
    async with Client(mcp) as client:
        args = {"task": "long one", "wait_seconds": 0.1}
        first = (await client.call_tool("browser_run_task", args)).structured_content
        assert first["done"] is False
        assert "browser_wait_task" in first["next_action"]
        final = (
            await client.call_tool("browser_wait_task", {"task_id": first["task_id"], "wait_seconds": 5})
        ).structured_content
    assert final["done"] is True
    assert final["status"] == "succeeded"


async def test_cancel_and_list():
    mcp, _ = create_agent_server(settings(), FakeRunner(steps=500, step_delay=0.02))
    async with Client(mcp) as client:
        args = {"task": "slow", "wait_seconds": 0}
        started = (await client.call_tool("browser_run_task", args)).structured_content
        await client.call_tool("browser_cancel_task", {"task_id": started["task_id"]})
        final = (
            await client.call_tool("browser_wait_task", {"task_id": started["task_id"], "wait_seconds": 5})
        ).structured_content
        listed = (await client.call_tool("browser_list_tasks", {})).structured_content
    assert final["status"] == "cancelled"
    assert listed["total"] == 1


async def test_unknown_task_is_tool_error():
    mcp, _ = create_agent_server(settings(), FakeRunner())
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("browser_get_task", {"task_id": "missing"})


async def test_domain_policy_cannot_be_widened():
    mcp, _ = create_agent_server(settings(browser_allowed_domains="example.com"), FakeRunner())
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "browser_run_task", {"task": "go somewhere", "allowed_domains": ["evil.com"]}
            )
