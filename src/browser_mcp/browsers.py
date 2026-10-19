"""Browser resources shared by both endpoints: a global Chromium limit and the profile factory."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Settings

if TYPE_CHECKING:
    from browser_use.browser import BrowserProfile


class PoolExhaustedError(RuntimeError):
    pass


class Lease:
    """One browser slot. `release()` is idempotent, so callers can release from any cleanup path."""

    def __init__(self, pool: BrowserPool):
        self._pool = pool
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._pool._release()


class BrowserPool:
    """Caps the number of live Chromium processes across agent tasks and direct sessions."""

    def __init__(self, size: int):
        self.size = size
        self._semaphore = asyncio.Semaphore(size)
        self._in_use = 0

    @property
    def in_use(self) -> int:
        return self._in_use

    async def acquire(self, timeout: float | None = None) -> Lease:
        """Wait for a free slot; `timeout=None` waits indefinitely."""
        try:
            async with asyncio.timeout(timeout):
                await self._semaphore.acquire()
        except TimeoutError:
            raise PoolExhaustedError(
                f"All {self.size} browsers are busy. Close an idle session or try again later."
            ) from None
        self._in_use += 1
        return Lease(self)

    @contextlib.asynccontextmanager
    async def slot(self, timeout: float | None = None) -> AsyncIterator[Lease]:
        lease = await self.acquire(timeout)
        try:
            yield lease
        finally:
            lease.release()

    def _release(self) -> None:
        self._in_use -= 1
        self._semaphore.release()


def build_profile(
    settings: Settings, workdir: Path, allowed_domains: list[str] | None = None, *, keep_alive: bool = False
) -> BrowserProfile:
    """Fresh, isolated profile under `workdir` with the server-wide navigation policy applied."""
    from browser_use.browser import BrowserProfile
    from browser_use.browser.profile import ProxySettings

    proxy = None
    if settings.browser_proxy:
        proxy = ProxySettings(server=settings.browser_proxy, bypass=settings.browser_proxy_bypass)

    return BrowserProfile(
        proxy=proxy,
        headless=settings.browser_headless,
        window_size=settings.window_size,
        executable_path=settings.browser_executable_path,
        user_data_dir=workdir / "profile",
        downloads_path=workdir / "downloads",
        allowed_domains=allowed_domains or settings.browser_allowed_domains,
        prohibited_domains=settings.browser_prohibited_domains,
        block_ip_addresses=settings.browser_block_ip_addresses,
        enable_default_extensions=settings.browser_enable_extensions,
        keep_alive=keep_alive,
    )


def resolve_domains(settings: Settings, requested: list[str] | None) -> list[str] | None:
    """Clients may narrow the server's domain policy, never widen it."""
    if not requested:
        return None
    allowed = settings.browser_allowed_domains
    if allowed:
        extra = [d for d in requested if d not in allowed]
        if extra:
            raise ValueError(f"Domains not permitted by server policy: {', '.join(extra)}")
    return requested
