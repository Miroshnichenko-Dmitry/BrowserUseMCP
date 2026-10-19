import asyncio
from typing import ClassVar

import dotenv
import pytest

from browser_mcp.config import Settings
from browser_mcp.sessions import ActionOutcome, PageState

# browser-use calls load_dotenv() as its modules get imported (lazily, possibly in the middle of
# a test), copying the developer's .env into os.environ. Tests never read .env.
dotenv.load_dotenv = lambda *args, **kwargs: False


@pytest.fixture(autouse=True)
def _isolate_from_environment(monkeypatch):
    """Settings come only from each test's arguments, not from the developer's shell."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


JPEG = bytes([0xFF, 0xD8]) + b"jpeg"


class FakeDriver:
    """In-memory browser: records actions, renders a tiny state with the current URL."""

    instances: ClassVar[list["FakeDriver"]] = []

    def __init__(self, session_id: str, fail_start: bool = False):
        self.session_id = session_id
        self.fail_start = fail_start
        self.actions: list[tuple[str, dict]] = []
        self.started = False
        self.closed = False
        self.last_url: str | None = None
        self.action_delay = 0.0
        self.close_delay = 0.0
        FakeDriver.instances.append(self)

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("chromium failed to start")
        self.started = True

    async def close(self) -> None:
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
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
