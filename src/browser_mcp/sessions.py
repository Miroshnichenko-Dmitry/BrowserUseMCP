"""The server's single browser and which agent may use it.

One Chromium, one agent at a time:

- The first agent to call a tool gets a fresh browser; all its calls go to that browser.
- Nobody else asking: the browser stays with that agent, however long it is idle.
- Another agent's call waits up to `busy_wait`. It takes the browser over once the current agent
  has been idle for `handover_idle` (or has held it for `max_age`, if set, as soon as its running
  call ends). The old browser is closed and its profile deleted; the newcomer gets a fresh one.
- `release()` (browser_close, or the agent's MCP session ending) frees the browser at once.

Independent from browser-use and FastMCP: the browser is behind a `Driver`, so this can be
unit-tested with a fake one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class PageState:
    text: str
    screenshot: bytes | None = None  # JPEG


class Driver(Protocol):
    """One browser. Actions are browser-use action names (`click`, `input`, `navigate`, ...)."""

    last_url: str | None

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def act(self, action: str, params: dict[str, object]) -> ActionOutcome: ...
    async def state(self, screenshot: bool = False) -> PageState: ...
    async def screenshot(self, full_page: bool = False) -> bytes: ...
    async def read(self) -> str: ...


DriverFactory = Callable[[str], Driver]


class BrowserBusyError(RuntimeError):
    """Another agent holds the browser."""


class SessionClosedError(RuntimeError):
    """The caller's browser was closed while its call was waiting."""


@dataclass(frozen=True, slots=True)
class SlotConfig:
    handover_idle: float = 30
    busy_wait: float = 20
    max_age: float = 0  # 0 = no limit
    close_timeout: float = 20
    remember_closed: int = 100  # owners told why their browser was closed


@dataclass(eq=False)
class Session:
    id: str
    owner: str
    driver: Driver
    created: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    calls: int = 0  # calls of the owner that are running or queued on `lock`
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    started: bool = False
    closed: bool = False
    notice: str | None = None  # told to the owner once, with the first result

    @property
    def idle_seconds(self) -> float:
        return 0.0 if self.calls else time.monotonic() - self.last_used

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created

    def take_notice(self) -> str | None:
        notice, self.notice = self.notice, None
        return notice


class BrowserSlot:
    def __init__(self, factory: DriverFactory, config: SlotConfig):
        self._factory = factory
        self._config = config
        self._session: Session | None = None
        self._changed = asyncio.Condition()
        self._waiting: deque[object] = deque()  # FIFO of other agents' calls
        self._retiring: set[asyncio.Task[None]] = set()
        self._closed_owners: OrderedDict[str, str] = OrderedDict()  # owner -> why it lost its browser
        self._shutting_down = False

    # -------------------------------------------------------------- queries
    @property
    def session(self) -> Session | None:
        return self._session

    def status(self) -> dict[str, object]:
        session = self._session
        if session is None:
            return {"state": "free", "waiting": len(self._waiting)}
        return {
            "state": "working" if session.calls else "idle",
            "owner": session.owner,
            "idle_seconds": round(session.idle_seconds, 1),
            "age_seconds": round(session.age_seconds, 1),
            "waiting": len(self._waiting),
        }

    # ------------------------------------------------------------- commands
    @contextlib.asynccontextmanager
    async def use(self, owner: str) -> AsyncIterator[Session]:
        """Exclusive access for one tool call of `owner`. Waits for, or takes over, the browser."""
        session = await self._claim(owner)
        try:
            async with session.lock:
                if session.closed:
                    raise SessionClosedError("Your browser was closed (browser_close or disconnect).")
                if not session.started:
                    await self._start(session)
                yield session
        finally:
            async with self._changed:
                session.calls -= 1
                session.last_used = time.monotonic()
                self._changed.notify_all()

    async def release(self, owner: str, reason: str) -> bool:
        """Close `owner`'s browser now (its own request). False if it holds none."""
        async with self._changed:
            session = self._session
            if session is None or session.owner != owner:
                return False
            self._retire(session, reason, tell_owner=False)
        return True

    async def shutdown(self) -> None:
        async with self._changed:
            self._shutting_down = True
            if self._session is not None:
                self._retire(self._session, "server is shutting down", tell_owner=False)
        if self._retiring:
            await asyncio.gather(*list(self._retiring), return_exceptions=True)

    # ------------------------------------------------------------ internals
    async def _claim(self, owner: str) -> Session:
        async with self._changed:
            if self._shutting_down:
                raise BrowserBusyError("Server is shutting down.")
            current = self._session
            if current is not None and current.owner == owner:
                current.calls += 1
                return current

            ticket = object()
            self._waiting.append(ticket)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._config.busy_wait
            try:
                while True:
                    if self._shutting_down:
                        raise BrowserBusyError("Server is shutting down.")
                    current = self._session
                    if current is not None and current.owner == owner:  # our parallel call got it
                        current.calls += 1
                        return current
                    first = self._waiting[0] is ticket
                    if first and current is None:
                        break
                    if first and current is not None:
                        reason = self._handover_reason(current)
                        if reason is not None:
                            self._retire(current, reason, tell_owner=True)
                            break
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise BrowserBusyError(self._busy_message(owner, current))
                    wake = remaining
                    if first and current is not None:
                        wake = min(wake, self._until_handover(current))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._changed.wait(), timeout=max(wake, 0.01))
            finally:
                self._waiting.remove(ticket)
                self._changed.notify_all()

            session_id = uuid.uuid4().hex
            session = Session(id=session_id, owner=owner, driver=self._factory(session_id), calls=1)
            session.notice = self._new_session_notice(owner)
            self._session = session
            return session

    async def _start(self, session: Session) -> None:
        if self._retiring:  # never run two Chromium processes at once
            await asyncio.gather(*list(self._retiring), return_exceptions=True)
        try:
            await session.driver.start()
        except BaseException:
            async with self._changed:
                if self._session is session:
                    self._session = None
                session.closed = True
                self._changed.notify_all()
            with contextlib.suppress(Exception):
                await session.driver.close()
            raise
        session.started = True
        logger.info("browser session opened", extra={"session_id": session.id, "owner": session.owner})

    def _handover_reason(self, current: Session) -> str | None:
        if current.calls:
            return None
        if current.idle_seconds >= self._config.handover_idle:
            return (
                f"another agent was waiting and you had been inactive for {current.idle_seconds:.0f}s "
                f"(limit {self._config.handover_idle:.0f}s)"
            )
        if self._config.max_age and current.age_seconds >= self._config.max_age:
            return (
                "another agent was waiting and your session reached its time limit "
                f"of {self._config.max_age:.0f}s"
            )
        return None

    def _until_handover(self, current: Session) -> float:
        if current.calls:
            return math.inf  # woken up when the call ends
        left = self._config.handover_idle - current.idle_seconds
        if self._config.max_age:
            left = min(left, self._config.max_age - current.age_seconds)
        return max(left, 0.0)

    def _retire(self, session: Session, reason: str, *, tell_owner: bool) -> None:
        """Take the browser away from its owner and close it in the background. Caller holds the lock."""
        if self._session is session:
            self._session = None
        session.closed = True
        if tell_owner:
            self._closed_owners[session.owner] = reason
            self._closed_owners.move_to_end(session.owner)
            while len(self._closed_owners) > self._config.remember_closed:
                self._closed_owners.popitem(last=False)
        task = asyncio.create_task(self._close_driver(session), name=f"close-browser-{session.id[:8]}")
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)
        self._changed.notify_all()
        logger.info("browser session closed", extra={"session_id": session.id, "reason": reason})

    async def _close_driver(self, session: Session) -> None:
        try:
            # Let a call that is still running finish, but never hang on a stuck browser.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._config.close_timeout), session.lock:
                    pass
            async with asyncio.timeout(self._config.close_timeout):
                await session.driver.close()
        except Exception:
            logger.warning(
                "browser session did not close cleanly", exc_info=True, extra={"session_id": session.id}
            )

    def _new_session_notice(self, owner: str) -> str:
        reason = self._closed_owners.pop(owner, None)
        if reason:
            return (
                f"NOTE: your previous browser was closed: {reason}. This is a new, empty browser: "
                "earlier pages, tabs and logins are gone."
            )
        return "NOTE: a new browser was started for you (empty profile)."

    def _busy_message(self, owner: str, current: Session | None) -> str:
        parts = []
        if owner in self._closed_owners:
            parts.append(f"Your browser was closed: {self._closed_owners[owner]}.")
        parts.append("The browser is in use by another agent (this server serves one agent at a time).")
        retry = self._config.handover_idle
        if current is not None and current.calls:
            parts.append("It is working right now.")
        elif current is not None:
            retry = self._until_handover(current)
            parts.append(
                f"It has been idle for {current.idle_seconds:.0f}s; the browser is handed over "
                f"after {self._config.handover_idle:.0f}s of inactivity."
            )
        ahead = len(self._waiting) - 1
        if ahead > 0:
            parts.append(f"{ahead} other call(s) are waiting too.")
        parts.append(f"Retry in about {max(math.ceil(retry), 1)}s.")
        return " ".join(parts)
