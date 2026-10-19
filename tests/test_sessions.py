import asyncio

import pytest

from browser_mcp.browsers import BrowserPool, PoolExhaustedError
from browser_mcp.sessions import SessionLimitError, SessionManager, SessionManagerConfig, SessionNotFoundError
from conftest import FakeDriver


def make_manager(pool_size: int = 2, **overrides) -> tuple[SessionManager, BrowserPool]:
    pool = BrowserPool(pool_size)
    config = SessionManagerConfig(**{"open_wait": 0.05, "cleanup_interval": 3600, **overrides})
    return SessionManager(FakeDriver, pool, config), pool


async def test_default_session_is_created_once_per_owner():
    manager, pool = make_manager()
    first, second = await asyncio.gather(manager.resolve("client-a"), manager.resolve("client-a"))
    assert first is second
    assert first.is_default and first.driver.started
    assert pool.in_use == 1
    other = await manager.resolve("client-b")
    assert other is not first
    assert pool.in_use == 2


async def test_explicit_session_reachable_from_another_owner():
    manager, _ = make_manager()
    session = await manager.open("old-connection")
    assert await manager.resolve("new-connection", session.id) is session
    with pytest.raises(SessionNotFoundError):
        await manager.resolve("new-connection", "missing")


async def test_pool_limit_is_shared_with_agent_tasks():
    manager, pool = make_manager(pool_size=1)
    session = await manager.resolve("a")
    with pytest.raises(PoolExhaustedError):
        await manager.resolve("b")

    # An agent task waits for the slot held by the session and gets it once the session closes.
    async def agent_task():
        async with pool.slot():
            return pool.in_use

    waiting = asyncio.create_task(agent_task())
    await asyncio.sleep(0.02)
    assert not waiting.done()
    await manager.close(session.id)
    assert await asyncio.wait_for(waiting, 1) == 1
    assert pool.in_use == 0


async def test_per_owner_limit():
    manager, _ = make_manager(pool_size=5, max_per_owner=2)
    await manager.open("a")
    await manager.open("a")
    with pytest.raises(SessionLimitError):
        await manager.open("a")


async def test_failed_start_releases_slot():
    pool = BrowserPool(1)
    manager = SessionManager(
        lambda sid, d: FakeDriver(sid, d, fail_start=True), pool, SessionManagerConfig(open_wait=0.05)
    )
    with pytest.raises(RuntimeError):
        await manager.open("a")
    assert pool.in_use == 0
    assert manager.count == 0


async def test_close_releases_slot_and_default():
    manager, pool = make_manager()
    session = await manager.resolve("a")
    assert await manager.close(session.id)
    assert session.driver.closed
    assert pool.in_use == 0
    assert not await manager.close(session.id)
    assert (await manager.resolve("a")) is not session  # a fresh default


async def test_actions_on_one_session_are_serialized():
    manager, _ = make_manager()
    order: list[str] = []

    async def act(tag: str):
        async with manager.use("a") as session:
            order.append(f"{tag}-start")
            await asyncio.sleep(0.02)
            order.append(f"{tag}-end")
            return session

    await asyncio.gather(act("one"), act("two"))
    assert order in (
        ["one-start", "one-end", "two-start", "two-end"],
        ["two-start", "two-end", "one-start", "one-end"],
    )


async def test_purge_idle_skips_busy_sessions():
    manager, pool = make_manager(idle_timeout=0.01)
    idle = await manager.resolve("a")
    busy = await manager.resolve("b")
    await asyncio.sleep(0.03)
    async with busy.lock:
        assert await manager.purge_idle() == 1
    assert idle.driver.closed and not busy.driver.closed
    assert pool.in_use == 1


async def test_shutdown_closes_everything():
    manager, pool = make_manager()
    await manager.start()
    await manager.resolve("a")
    await manager.open("a")
    await manager.shutdown()
    assert manager.count == 0
    assert pool.in_use == 0
    assert all(d.closed for d in FakeDriver.instances)
    with pytest.raises(SessionLimitError):
        await manager.open("a")
