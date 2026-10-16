import asyncio

import pytest

from browser_mcp.models import TaskOutcome, TaskProgress
from browser_mcp.tasks import TaskSpec


class FakeRunner:
    """Simulates an agent: N steps of `step_delay`, then returns `outcome`."""

    def __init__(
        self,
        steps: int = 3,
        step_delay: float = 0.01,
        outcome: TaskOutcome | None = None,
        error: Exception | None = None,
        ignore_stop: bool = False,
    ):
        self.steps = steps
        self.step_delay = step_delay
        self.outcome = outcome or TaskOutcome(done=True, success=True, result="ok", steps=steps)
        self.error = error
        self.ignore_stop = ignore_stop
        self.started = asyncio.Event()
        self.calls: list[TaskSpec] = []

    async def __call__(self, task_id, spec, report, stop_requested):
        self.calls.append(spec)
        self.started.set()
        for step in range(1, self.steps + 1):
            if stop_requested.is_set() and not self.ignore_stop:
                return TaskOutcome(done=False, success=None, error="Agent stopped", steps=step)
            await asyncio.sleep(self.step_delay)
            await report(TaskProgress(step=step, max_steps=spec.max_steps, last_goal=f"goal {step}"))
        if self.error:
            raise self.error
        return self.outcome


@pytest.fixture
def fake_runner():
    return FakeRunner()
