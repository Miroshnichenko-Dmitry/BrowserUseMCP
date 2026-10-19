import asyncio
from typing import ClassVar

import pytest

from browser_mcp.models import TaskOutcome, TaskProgress
from browser_mcp.sessions import ActionOutcome, PageState
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


JPEG = bytes([0xFF, 0xD8]) + b"jpeg"


class FakeDriver:
    """In-memory browser: records actions, renders a tiny state with the current URL."""

    instances: ClassVar[list["FakeDriver"]] = []

    def __init__(self, session_id: str, allowed_domains: list[str] | None, fail_start: bool = False):
        self.session_id = session_id
        self.allowed_domains = allowed_domains
        self.fail_start = fail_start
        self.actions: list[tuple[str, dict]] = []
        self.started = False
        self.closed = False
        self.last_url: str | None = None
        self.action_delay = 0.0
        FakeDriver.instances.append(self)

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("chromium failed to start")
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def act(self, action: str, params: dict) -> ActionOutcome:
        if self.action_delay:
            await asyncio.sleep(self.action_delay)
        self.actions.append((action, params))
        if action == "navigate":
            self.last_url = params["url"]
        if params.get("index") == 999:
            return ActionOutcome(False, "Element index 999 not available")
        return ActionOutcome(True, f"{action} ok")

    async def state(self, screenshot: bool = False) -> PageState:
        text = f"URL: {self.last_url or 'about:blank'}\nPage:\n[1]<button>Go</button>"
        return PageState(text=text, screenshot=JPEG if screenshot else None)

    async def screenshot(self, full_page: bool = False) -> bytes:
        return JPEG

    async def read(self) -> str:
        return "# Title\n" + "x" * 50


@pytest.fixture(autouse=True)
def _reset_fake_drivers():
    FakeDriver.instances.clear()
