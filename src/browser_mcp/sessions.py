"""Browser sessions for the low-level endpoint: ownership, per-session locking, idle expiry.

Like `tasks.py`, independent from browser-use and FastMCP. The browser itself is behind a
`Driver`, so the manager can be unit-tested with a fake one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from .browsers import BrowserPool, Lease

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


DriverFactory = Callable[[str, list[str] | None], Driver]


class SessionNotFoundError(KeyError):
    pass


class SessionLimitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionManagerConfig:
    idle_timeout: float = 600
    max_per_owner: int = 3
    open_wait: float = 15
    close_timeout: float = 20
    cleanup_interval: float = 30


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(eq=False)
class Session:
    id: str
    owner: str
    driver: Driver
    lease: Lease
    allowed_domains: list[str] | None
    is_default: bool
    created_at: datetime = field(default_factory=_now)
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used


class SessionManager:
    def __init__(self, factory: DriverFactory, pool: BrowserPool, config: SessionManagerConfig):
        self._factory = factory
        self._pool = pool
        self._config = config
        self._sessions: dict[str, Session] = {}
        self._defaults: dict[str, str] = {}  # owner -> default session id
        self._owner_locks: dict[str, asyncio.Lock] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closing = False

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="session-cleanup")

    async def shutdown(self) -> None:
        self._closing = True
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        await asyncio.gather(*(self.close(sid) for sid in list(self._sessions)), return_exceptions=True)

    # -------------------------------------------------------------- queries
    @property
    def count(self) -> int:
        return len(self._sessions)

    def list(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at)

    # ------------------------------------------------------------- commands
    async def open(
        self, owner: str, allowed_domains: list[str] | None = None, default: bool = False
    ) -> Session:
        if self._closing:
            raise SessionLimitError("Server is shutting down")
        owned = sum(1 for s in self._sessions.values() if s.owner == owner)
        if owned >= self._config.max_per_owner:
            raise SessionLimitError(
                f"You already have {owned} browser sessions (limit {self._config.max_per_owner}). "
                "Close one with browser_close."
            )
        lease = await self._pool.acquire(self._config.open_wait)
        session_id = uuid.uuid4().hex
        driver = self._factory(session_id, allowed_domains)
        try:
            await driver.start()
        except BaseException:
            lease.release()
            with contextlib.suppress(Exception):
                await driver.close()
            raise
        session = Session(
            id=session_id,
            owner=owner,
            driver=driver,
            lease=lease,
            allowed_domains=allowed_domains,
            is_default=default,
        )
        self._sessions[session_id] = session
        if default:
            self._defaults[owner] = session_id
        logger.info("browser session opened", extra={"session_id": session_id, "default": default})
        return session

    async def resolve(self, owner: str, session_id: str | None = None) -> Session:
        """Explicit id → that session (from any owner: ids are unguessable, so reconnects work).
        No id → the owner's default session, created on first use."""
        if session_id:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            return session
        lock = self._owner_locks.setdefault(owner, asyncio.Lock())
        async with lock:  # two parallel first calls must not start two browsers
            existing = self._sessions.get(self._defaults.get(owner, ""))
            if existing is not None:
                return existing
            return await self.open(owner, default=True)

    @contextlib.asynccontextmanager
    async def use(self, owner: str, session_id: str | None = None) -> AsyncIterator[Session]:
        """Exclusive access for one tool call: actions on a session never interleave."""
        session = await self.resolve(owner, session_id)
        async with session.lock:
            if session.closed:
                raise SessionNotFoundError(session.id)
            session.touch()
            try:
                yield session
            finally:
                session.touch()

    async def close(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.closed = True
        if self._defaults.get(session.owner) == session_id:
            del self._defaults[session.owner]
        if not any(s.owner == session.owner for s in self._sessions.values()):
            self._owner_locks.pop(session.owner, None)
        try:
            # Let a running action finish, but never hang shutdown on a stuck browser.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._config.close_timeout):
                    await session.lock.acquire()
                    session.lock.release()
            async with asyncio.timeout(self._config.close_timeout):
                await session.driver.close()
        except Exception:
            logger.warning(
                "browser session did not close cleanly", exc_info=True, extra={"session_id": session_id}
            )
        finally:
            session.lease.release()
        logger.info("browser session closed", extra={"session_id": session_id})
        return True

    async def purge_idle(self) -> int:
        idle = [
            s.id
            for s in self._sessions.values()
            if s.idle_seconds > self._config.idle_timeout and not s.lock.locked()
        ]
        for session_id in idle:
            logger.info("closing idle browser session", extra={"session_id": session_id})
            await self.close(session_id)
        return len(idle)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.cleanup_interval)
            try:
                await self.purge_idle()
            except Exception:
                logger.exception("idle session cleanup failed")
