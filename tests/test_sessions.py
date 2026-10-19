"""BrowserSlot: one browser, one agent at a time, handover only when another agent is waiting."""

import asyncio
import time

import pytest

from browser_mcp.sessions import BrowserBusyError, BrowserSlot, SessionClosedError, SlotConfig
from conftest import FakeDriver


def make_slot(**overrides) -> BrowserSlot:
    config = SlotConfig(**{"handover_idle": 0.2, "busy_wait": 0.05, "close_timeout": 1, **overrides})
    return BrowserSlot(FakeDriver, config)


async def call(slot: BrowserSlot, owner: str, url: str = "https://x.test") -> FakeDriver:
    async with slot.use(owner) as session:
        await session.driver.act("navigate", {"url": url})
        return session.driver


async def test_first_call_starts_one_browser_for_its_owner():
    slot = make_slot()
    first, second = await asyncio.gather(call(slot, "a"), call(slot, "a"))
    assert first is second and first.started
    assert len(FakeDriver.instances) == 1
    assert slot.status()["owner"] == "a"


async def test_idle_owner_keeps_browser_while_nobody_waits():
    slot = make_slot(handover_idle=0.05)
    driver = await call(slot, "a")
    await asyncio.sleep(0.15)  # idle well past the handover limit
    assert await call(slot, "a") is driver
    assert not driver.closed


async def test_newcomer_gets_busy_while_owner_was_active_recently():
    slot = make_slot(handover_idle=10)
    await call(slot, "a")
    with pytest.raises(BrowserBusyError, match="in use by another agent"):
        await call(slot, "b")
    assert slot.status()["owner"] == "a"


async def test_newcomer_takes_over_after_owner_idles():
    slot = make_slot(handover_idle=0.1, busy_wait=1)
    old = await call(slot, "a")
    started = time.monotonic()
    new = await call(slot, "b")
    assert 0.05 < time.monotonic() - started < 0.5  # waited for the idle limit, not for busy_wait
    assert new is not old and new.started
    await asyncio.sleep(0)  # let the background close run
    assert old.closed
    assert slot.status()["owner"] == "b"


async def test_previous_owner_is_told_its_browser_was_closed():
    slot = make_slot(handover_idle=0.05, busy_wait=0.2)
    await call(slot, "a")
    async with slot.use("b"):  # b took over and is working
        with pytest.raises(BrowserBusyError, match="Your browser was closed: another agent was waiting"):
            await call(slot, "a")
    async with slot.use("a") as session:  # b idles, a takes it back
        assert "previous browser was closed" in session.take_notice()
        assert session.take_notice() is None  # told once


async def test_owner_in_the_middle_of_a_call_is_not_taken_over():
    slot = make_slot(handover_idle=0, busy_wait=0.1)
    async with slot.use("a"):
        with pytest.raises(BrowserBusyError, match="working right now"):
            await call(slot, "b")
    assert await call(slot, "b")  # free to take once a's call is over


async def test_session_time_limit_lets_a_waiting_agent_in_despite_activity():
    slot = make_slot(handover_idle=100, busy_wait=1, max_age=0.1)
    await call(slot, "a")
    await asyncio.sleep(0.12)
    await call(slot, "a")  # a is active, but over its time limit
    new = await call(slot, "b")
    assert slot.status()["owner"] == "b" and new.started


async def test_time_limit_alone_does_not_close_the_browser():
    slot = make_slot(handover_idle=100, max_age=0.05)
    driver = await call(slot, "a")
    await asyncio.sleep(0.1)
    assert await call(slot, "a") is driver


async def test_release_frees_the_browser_at_once():
    slot = make_slot(handover_idle=100)
    driver = await call(slot, "a")
    assert not await slot.release("b", "not theirs")
    assert await slot.release("a", "done")
    assert slot.status()["state"] == "free"
    async with slot.use("b") as session:  # no waiting: the browser is free
        assert session.driver is not driver and driver.closed
        assert session.take_notice() == "NOTE: a new browser was started for you (empty profile)."


async def test_queued_call_of_released_owner_fails():
    slot = make_slot(handover_idle=100)
    await call(slot, "a")

    async def slow_call():
        async with slot.use("a") as session:
            await asyncio.sleep(0.1)
            return session

    running = asyncio.create_task(slow_call())
    await asyncio.sleep(0.01)
    queued = asyncio.create_task(call(slot, "a"))
    await asyncio.sleep(0.01)
    await slot.release("a", "done")
    await running  # the call in progress finishes normally
    with pytest.raises(SessionClosedError):
        await queued


async def test_waiting_agents_are_served_in_order():
    slot = make_slot(handover_idle=0.05, busy_wait=1)
    await call(slot, "a")
    first = asyncio.create_task(call(slot, "b"))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(call(slot, "c"))
    await first
    assert slot.status()["owner"] == "b"
    await second  # c waits for b to idle in turn
    assert slot.status()["owner"] == "c"


async def test_old_browser_is_closed_before_the_new_one_starts():
    slot = make_slot(handover_idle=0, busy_wait=1)
    old = await call(slot, "a")
    old.close_delay = 0.1
    new = await call(slot, "b")
    assert old.closed and new.started


async def test_failed_start_frees_the_slot():
    slot = BrowserSlot(lambda sid: FakeDriver(sid, fail_start=True), SlotConfig(busy_wait=0.05))
    with pytest.raises(RuntimeError, match="failed to start"):
        await call(slot, "a")
    assert slot.status()["state"] == "free"
    assert FakeDriver.instances[0].closed


async def test_shutdown_closes_the_browser_and_refuses_new_calls():
    slot = make_slot()
    driver = await call(slot, "a")
    await slot.shutdown()
    assert driver.closed
    with pytest.raises(BrowserBusyError, match="shutting down"):
        await call(slot, "a")


async def test_shutdown_does_not_hand_the_browser_to_a_waiting_agent():
    slot = make_slot(handover_idle=100, busy_wait=1)
    await call(slot, "a")
    waiting = asyncio.create_task(call(slot, "b"))
    await asyncio.sleep(0.01)
    await slot.shutdown()
    with pytest.raises(BrowserBusyError, match="shutting down"):
        await waiting
    assert len(FakeDriver.instances) == 1
