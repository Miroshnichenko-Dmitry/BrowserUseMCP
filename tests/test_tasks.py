import asyncio

import pytest

from browser_mcp.models import TaskOutcome, TaskStatus
from browser_mcp.tasks import QueueFullError, TaskManager, TaskManagerConfig, TaskNotFoundError, TaskSpec
from conftest import FakeRunner


def make_manager(runner, **overrides) -> TaskManager:
    config = TaskManagerConfig(**{"cleanup_interval": 3600, **overrides})
    return TaskManager(runner, config)


SPEC = TaskSpec(task="open example.com", max_steps=10)


async def test_success_reports_done_and_result():
    manager = make_manager(FakeRunner(steps=3))
    view = manager.submit(SPEC)
    assert view.status in (TaskStatus.QUEUED, TaskStatus.RUNNING)
    assert view.done is False

    final = await manager.wait(view.task_id, timeout=5)
    assert final.status is TaskStatus.SUCCEEDED
    assert final.done is True and final.success is True
    assert final.result == "ok"
    assert final.progress.step == 3
    assert final.duration_seconds is not None


async def test_wait_returns_running_snapshot_on_timeout_and_streams_progress():
    manager = make_manager(FakeRunner(steps=50, step_delay=0.02))
    view = manager.submit(SPEC)
    updates = []

    async def on_update(v):
        updates.append(v.progress.step)

    snapshot = await manager.wait(view.task_id, timeout=0.2, on_update=on_update)
    assert snapshot.done is False
    assert snapshot.status is TaskStatus.RUNNING
    assert "browser_wait_task" in (snapshot.next_action or "")
    assert len(updates) >= 2 and updates == sorted(updates)
    await manager.cancel(view.task_id)
    await manager.wait(view.task_id, timeout=5)


async def test_not_done_is_failure_even_with_content():
    outcome = TaskOutcome(done=False, success=None, result=None, error="step limit", steps=10)
    manager = make_manager(FakeRunner(steps=1, outcome=outcome))
    view = manager.submit(SPEC)
    final = await manager.wait(view.task_id, timeout=5)
    assert final.status is TaskStatus.FAILED
    assert final.success is False
    assert final.error == "step limit"


async def test_agent_reported_failure():
    outcome = TaskOutcome(done=True, success=False, result="captcha", steps=2)
    manager = make_manager(FakeRunner(steps=1, outcome=outcome))
    final = await manager.wait(manager.submit(SPEC).task_id, timeout=5)
    assert final.status is TaskStatus.FAILED
    assert final.result == "captcha"


async def test_runner_exception_becomes_failed():
    manager = make_manager(FakeRunner(steps=1, error=RuntimeError("browser crashed")))
    final = await manager.wait(manager.submit(SPEC).task_id, timeout=5)
    assert final.status is TaskStatus.FAILED
    assert "browser crashed" in (final.error or "")


async def test_timeout():
    manager = make_manager(FakeRunner(steps=100, step_delay=0.05), task_timeout=0.1)
    final = await manager.wait(manager.submit(SPEC).task_id, timeout=5)
    assert final.status is TaskStatus.TIMED_OUT


async def test_cooperative_cancel():
    runner = FakeRunner(steps=100, step_delay=0.02)
    manager = make_manager(runner, cancel_grace=5)
    view = manager.submit(SPEC)
    await runner.started.wait()
    await manager.cancel(view.task_id)
    final = await manager.wait(view.task_id, timeout=5)
    assert final.status is TaskStatus.CANCELLED


async def test_hard_cancel_when_runner_ignores_stop():
    runner = FakeRunner(steps=1000, step_delay=0.02, ignore_stop=True)
    manager = make_manager(runner, cancel_grace=0.1)
    view = manager.submit(SPEC)
    await runner.started.wait()
    await manager.cancel(view.task_id)
    final = await manager.wait(view.task_id, timeout=5)
    assert final.status is TaskStatus.CANCELLED


async def test_concurrency_limit_and_queue_position():
    runner = FakeRunner(steps=5, step_delay=0.02)
    manager = make_manager(runner, max_concurrent=1, max_queued=2)
    first = manager.submit(SPEC)
    second = manager.submit(SPEC)
    third = manager.submit(SPEC)
    await asyncio.sleep(0)  # let the first worker grab the semaphore
    assert manager.get(second.task_id).queue_position == 1
    assert manager.get(third.task_id).queue_position == 2
    with pytest.raises(QueueFullError):
        manager.submit(SPEC)
    for view in (first, second, third):
        assert (await manager.wait(view.task_id, timeout=5)).status is TaskStatus.SUCCEEDED


async def test_cancel_queued_task():
    runner = FakeRunner(steps=20, step_delay=0.02)
    manager = make_manager(runner, max_concurrent=1)
    running = manager.submit(SPEC)
    queued = manager.submit(SPEC)
    await asyncio.sleep(0)
    await manager.cancel(queued.task_id)
    final = await manager.wait(queued.task_id, timeout=5)
    assert final.status is TaskStatus.CANCELLED
    assert final.started_at is None
    await manager.cancel(running.task_id)
    await manager.wait(running.task_id, timeout=5)


async def test_unknown_task():
    manager = make_manager(FakeRunner())
    with pytest.raises(TaskNotFoundError):
        manager.get("nope")


async def test_purge_ttl_and_cap():
    manager = make_manager(FakeRunner(steps=1, step_delay=0), result_ttl=1000, max_stored=2)
    ids = [manager.submit(SPEC).task_id for _ in range(4)]
    for task_id in ids:
        await manager.wait(task_id, timeout=5)
    removed = manager.purge()
    assert removed == 2
    assert {v.task_id for v in manager.list()} == set(ids[2:])


async def test_shutdown_stops_running_tasks():
    runner = FakeRunner(steps=1000, step_delay=0.01)
    manager = make_manager(runner)
    await manager.start()
    view = manager.submit(SPEC)
    await runner.started.wait()
    await manager.shutdown(timeout=2)
    assert manager.get(view.task_id).status is TaskStatus.CANCELLED
    with pytest.raises(QueueFullError):
        manager.submit(SPEC)
