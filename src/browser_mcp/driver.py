"""browser-use backed `Driver`: one BrowserSession, actions via browser-use `Tools`.

Actions go through the same action registry the agent uses, so clicks, typing, scrolling and
navigation get the library's error handling and domain policy checks for free.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Settings
from .sessions import ActionOutcome, PageState

if TYPE_CHECKING:
    from browser_use import BrowserSession, Tools
    from browser_use.browser import BrowserProfile
    from browser_use.browser.views import BrowserStateSummary

logger = logging.getLogger(__name__)


class BrowserUseDriver:
    def __init__(self, settings: Settings, session_id: str):
        self._settings = settings
        self._workdir = Path(tempfile.mkdtemp(prefix=f"browser-session-{session_id[:8]}-"))
        self._session: BrowserSession | None = None
        self._tools: Tools | None = None
        self.last_url: str | None = None

    async def start(self) -> None:
        from browser_use import BrowserSession, Tools

        profile = build_profile(self._settings, self._workdir)
        self._session = BrowserSession(browser_profile=profile)
        await self._session.start()
        self._tools = Tools()
        self._tools.set_coordinate_clicking(True)

    async def close(self) -> None:
        try:
            if self._session is not None:
                await self._session.kill()
        finally:
            self._session = None
            await asyncio.to_thread(shutil.rmtree, self._workdir, True)

    @property
    def _browser(self) -> BrowserSession:
        if self._session is None:
            raise RuntimeError("Browser session is not started")
        return self._session

    async def act(self, action: str, params: dict[str, Any]) -> ActionOutcome:
        assert self._tools is not None
        call = getattr(self._tools, action)
        try:
            async with asyncio.timeout(self._settings.browser_action_timeout_seconds):
                result = await call(browser_session=self._browser, **params)
        except TimeoutError:
            return ActionOutcome(
                False, f"{action} timed out after {self._settings.browser_action_timeout_seconds:.0f}s"
            )
        except Exception as exc:  # invalid params or a browser failure the registry did not wrap
            return ActionOutcome(False, f"{type(exc).__name__}: {exc}")
        if result.error:
            return ActionOutcome(False, result.error.strip())
        message = result.extracted_content or result.long_term_memory or "done"
        return ActionOutcome(True, message.strip())

    async def state(self, screenshot: bool = False) -> PageState:
        summary = await self._browser.get_browser_state_summary(include_screenshot=screenshot)
        self.last_url = summary.url
        text = _format_state(
            summary, self._browser.agent_focus_target_id, self._settings.browser_state_max_chars
        )
        image = None
        if screenshot and summary.screenshot:
            png_b64 = await self._highlight(summary)
            image = _to_jpeg(base64.b64decode(png_b64), self._settings.browser_screenshot_quality)
        return PageState(text=text, screenshot=image)

    async def _highlight(self, summary: BrowserStateSummary) -> str:
        """Draw the [index] boxes onto the screenshot so it matches the text state."""
        from browser_use.browser.python_highlights import create_highlighted_screenshot_async

        assert summary.screenshot is not None
        try:
            cdp = await self._browser.get_or_create_cdp_session(target_id=None, focus=False)
            return await create_highlighted_screenshot_async(
                summary.screenshot, summary.dom_state.selector_map, cdp
            )
        except Exception:
            logger.debug("highlighting failed, returning a plain screenshot", exc_info=True)
            return summary.screenshot

    async def screenshot(self, full_page: bool = False) -> bytes:
        return await self._browser.take_screenshot(
            full_page=full_page, format="jpeg", quality=self._settings.browser_screenshot_quality
        )

    async def read(self) -> str:
        from browser_use.dom.markdown_extractor import extract_clean_markdown

        content, _ = await extract_clean_markdown(browser_session=self._browser, extract_links=True)
        return content


def build_profile(settings: Settings, workdir: Path) -> BrowserProfile:
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
        allowed_domains=settings.browser_allowed_domains,
        prohibited_domains=settings.browser_prohibited_domains,
        block_ip_addresses=settings.browser_block_ip_addresses,
        enable_default_extensions=settings.browser_enable_extensions,
        keep_alive=True,
    )


def _format_state(summary: BrowserStateSummary, focus_target: str | None, max_chars: int) -> str:
    from browser_use.dom.views import DEFAULT_INCLUDE_ATTRIBUTES

    lines = [f"URL: {summary.url}", f"Title: {summary.title}"]
    if len(summary.tabs) > 1:
        tabs = []
        for tab in summary.tabs:
            marker = " (current)" if tab.target_id == focus_target else ""
            tabs.append(f"[{tab.target_id[-4:]}] {tab.title[:60] or tab.url[:60]}{marker}")
        lines.append("Tabs: " + "; ".join(tabs))
    info = summary.page_info
    if info:
        lines.append(
            f"Viewport {info.viewport_width}x{info.viewport_height}; "
            f"{info.pixels_above}px above, {info.pixels_below}px below the visible area"
        )
    if summary.is_pdf_viewer:
        lines.append("This is a PDF viewer: use browser_read to get the text.")
    if summary.closed_popup_messages:
        lines.append("Auto-closed dialogs: " + " | ".join(summary.closed_popup_messages[-3:]))
    if summary.browser_errors:
        lines.append("Browser errors: " + " | ".join(summary.browser_errors[-3:]))

    body = summary.dom_state.llm_representation(include_attributes=DEFAULT_INCLUDE_ATTRIBUTES).strip()
    if not body:
        body = "(page is empty or still loading)"
    elif len(body) > max_chars:
        body = (
            body[:max_chars] + f"\n…[state truncated, {len(body) - max_chars} more chars: "
            "scroll, or use browser_read for text]"
        )
    lines += ["", "Page (interactive elements are marked [index]):", body]
    return "\n".join(lines)


def _to_jpeg(png: bytes, quality: int) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
