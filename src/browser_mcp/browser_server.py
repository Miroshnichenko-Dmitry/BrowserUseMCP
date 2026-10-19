"""Browser endpoint (/mcp/browser): low-level browser control driven by the client's own model.

Every action returns the fresh page state, so the client never needs a separate "look" call
between actions. Tool results are plain text (no structured duplicate) to keep the client's
context small.
"""

# NOTE: no `from __future__ import annotations` here: FastMCP introspects real annotations.
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.lifespan import lifespan
from fastmcp.utilities.types import Image
from pydantic import Field

from . import __version__
from .config import Settings
from .sessions import ActionOutcome, BrowserBusyError, BrowserSlot, Session, SessionClosedError

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Low-level browser control: you drive a real Chromium step by step.

- This server has one browser and serves one agent at a time. Your first call opens a fresh
  browser for you, and it stays yours while you work. If another agent is waiting and you have
  been inactive for {idle:.0f}s, your browser is closed and handed over: do not pause long between
  steps. If the browser is busy with another agent, tools fail with a "busy" error: retry later.
- Every action returns the new page state: URL, title, tabs and the page content where
  interactive elements are marked [index]. Use those indexes with browser_click, browser_type,
  browser_select_option. Indexes are valid only for the most recent state you received.
- browser_read returns the whole page as markdown (for reading long content).
  browser_find searches the page text. browser_screenshot shows the layout when text is not enough.
- Call browser_close when you are done, so the next agent does not have to wait.
"""

Index = Annotated[int, Field(ge=0, description="Element [index] from the latest page state.")]

_ACT = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
_READ = {"readOnlyHint": True, "openWorldHint": True}


def create_browser_server(
    settings: Settings, slot: BrowserSlot, auth: TokenVerifier | None = None
) -> FastMCP:
    @lifespan
    async def app_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"browser": slot}
        finally:
            logger.info("shutting down, closing the browser")
            await slot.shutdown()

    mcp = FastMCP(
        name="browser-use-browser",
        instructions=INSTRUCTIONS.format(idle=settings.browser_handover_idle_seconds),
        version=__version__,
        auth=auth,
        lifespan=app_lifespan,
        mask_error_details=True,
    )

    def tool(annotations: dict[str, Any]):
        # output_schema=None: results go out once, as text, not duplicated as structured content.
        return mcp.tool(annotations=annotations, output_schema=None)

    @contextlib.asynccontextmanager
    async def use(ctx: Context) -> AsyncIterator[Session]:
        """The caller's browser for one tool call; busy / closed become tool errors."""
        try:
            async with slot.use(_owner(ctx)) as session:
                yield session
        except (BrowserBusyError, SessionClosedError) as exc:
            raise ToolError(str(exc)) from exc

    async def run(ctx: Context, action: str, params: dict[str, Any]) -> str:
        """Run one browser-use action and return its outcome followed by the new page state."""
        async with use(ctx) as session:
            outcome = await session.driver.act(action, params)
            return await _with_state(session, outcome)

    async def _with_state(session: Session, outcome: ActionOutcome) -> str:
        # Let the page react (navigation, XHR) before reading it back.
        await asyncio.sleep(0.3)
        state = await session.driver.state()
        status = "OK" if outcome.ok else "FAILED"
        return _noted(session, f"{status}: {outcome.message}\n\n{state.text}")

    # ------------------------------------------------------------ session
    @tool({"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
    async def browser_close(ctx: Context) -> str:
        """Finish: close your browser and delete its profile, cookies and downloads, so the next
        agent can start right away. Call it when your work is done."""
        if await slot.release(_owner(ctx), "closed by browser_close"):
            return "Browser closed. The server is free for the next agent."
        return "You have no open browser."

    # ------------------------------------------------------------ navigation
    @tool(_ACT)
    async def browser_navigate(
        ctx: Context,
        url: Annotated[str, Field(min_length=1, max_length=4000)],
        new_tab: bool = False,
    ) -> str:
        """Open a URL in the current tab (or a new one)."""
        return await run(ctx, "navigate", {"url": url, "new_tab": new_tab})

    @tool(_ACT)
    async def browser_back(ctx: Context) -> str:
        """Go back in the current tab's history."""
        return await run(ctx, "go_back", {})

    @tool(_ACT)
    async def browser_tab(
        ctx: Context,
        tab_id: Annotated[
            str, Field(min_length=4, max_length=4, description="4-char id from the Tabs line.")
        ],
        close: Annotated[bool, Field(description="Close the tab instead of switching to it.")] = False,
    ) -> str:
        """Switch to a tab, or close it."""
        return await run(ctx, "close" if close else "switch", {"tab_id": tab_id})

    # ------------------------------------------------------------ interaction
    @tool(_ACT)
    async def browser_click(
        ctx: Context,
        index: Annotated[int | None, Field(ge=0, description="Element [index]. Preferred.")] = None,
        x: Annotated[int | None, Field(ge=0, description="Viewport X, only if there is no index.")] = None,
        y: Annotated[int | None, Field(ge=0, description="Viewport Y, together with x.")] = None,
    ) -> str:
        """Click an element by [index], or at viewport coordinates (x, y) seen on a screenshot."""
        if index is not None:
            params: dict[str, Any] = {"index": index}
        elif x is not None and y is not None:
            params = {"coordinate_x": x, "coordinate_y": y}
        else:
            raise ToolError("Pass `index`, or both `x` and `y`.")
        return await run(ctx, "click", params)

    @tool(_ACT)
    async def browser_type(
        ctx: Context,
        index: Index,
        text: Annotated[str, Field(max_length=10000)],
        clear: Annotated[bool, Field(description="Replace the current value (false = append).")] = True,
        submit: Annotated[bool, Field(description="Press Enter after typing.")] = False,
    ) -> str:
        """Type into an input or textarea. text="" with clear=true empties the field."""
        async with use(ctx) as session:
            outcome = await session.driver.act("input", {"index": index, "text": text, "clear": clear})
            if outcome.ok and submit:
                pressed = await session.driver.act("send_keys", {"keys": "Enter"})
                outcome = ActionOutcome(pressed.ok, f"{outcome.message}; Enter: {pressed.message}")
            return await _with_state(session, outcome)

    @tool(_ACT)
    async def browser_press_keys(
        ctx: Context,
        keys: Annotated[
            str, Field(min_length=1, max_length=200, description="e.g. Enter, Escape, Control+a")
        ],
    ) -> str:
        """Press a key or shortcut in the focused element."""
        return await run(ctx, "send_keys", {"keys": keys})

    @tool(_ACT)
    async def browser_select_option(
        ctx: Context,
        index: Index,
        option: Annotated[str, Field(min_length=1, description="Exact visible text or value of the option.")],
    ) -> str:
        """Choose an option in a <select> or custom dropdown.

        On a wrong option the error lists the valid ones.
        """
        return await run(ctx, "select_dropdown", {"index": index, "text": option})

    @tool(_ACT)
    async def browser_scroll(
        ctx: Context,
        direction: Literal["down", "up"] = "down",
        pages: Annotated[float, Field(gt=0, le=20, description="1 = one screen, 0.5 = half.")] = 1.0,
        index: Annotated[int | None, Field(ge=0, description="Scroll inside this element instead.")] = None,
    ) -> str:
        """Scroll the page (or a scrollable element)."""
        params: dict[str, Any] = {"down": direction == "down", "pages": pages}
        if index is not None:
            params["index"] = index
        return await run(ctx, "scroll", params)

    @tool(_READ)
    async def browser_wait(
        ctx: Context,
        seconds: Annotated[float, Field(gt=0, le=30)] = 2,
    ) -> str:
        """Wait for the page to load or update, then return its state."""
        async with use(ctx) as session:
            await asyncio.sleep(seconds)
            return await _with_state(session, ActionOutcome(True, f"waited {seconds:g}s"))

    # ------------------------------------------------------------ reading
    @tool(_READ)
    async def browser_snapshot(
        ctx: Context,
        screenshot: Annotated[
            bool, Field(description="Also return a screenshot with the [index] boxes drawn on it.")
        ] = False,
    ) -> str | list[Any]:
        """Return the current page state without doing anything."""
        async with use(ctx) as session:
            state = await session.driver.state(screenshot=screenshot)
            text = _noted(session, state.text)
        if state.screenshot:
            return [text, Image(data=state.screenshot, format="jpeg")]
        return text  # a list of plain strings would be sent as one JSON-encoded text

    @tool(_READ)
    async def browser_screenshot(ctx: Context, full_page: bool = False) -> list[Any]:
        """Screenshot of the visible area (or the whole page). Coordinates match browser_click x/y."""
        async with use(ctx) as session:
            data = await session.driver.screenshot(full_page=full_page)
            caption = _noted(session, f"Screenshot of {session.driver.last_url or 'the current page'}")
        return [caption, Image(data=data, format="jpeg")]

    @tool(_READ)
    async def browser_read(
        ctx: Context,
        start: Annotated[int, Field(ge=0, description="Character offset, to continue a long page.")] = 0,
    ) -> str:
        """Return the page content as markdown, with links. Long pages come in chunks: pass `start`."""
        async with use(ctx) as session:
            text = await session.driver.read()
            url = session.driver.last_url
            notice = session.take_notice()
        limit = settings.browser_read_max_chars
        chunk = text[start : start + limit]
        end = start + len(chunk)
        header = f"URL: {url or '-'}\nChars {start}-{end} of {len(text)}"
        if end < len(text):
            header += f" (more: call browser_read with start={end})"
        if notice:
            header = f"{notice}\n\n{header}"
        return f"{header}\n\n{chunk}"

    @tool(_READ)
    async def browser_find(
        ctx: Context,
        pattern: Annotated[str, Field(min_length=1, max_length=500)],
        regex: bool = False,
    ) -> str:
        """Find text on the page (like Ctrl+F) and return the matches with surrounding context."""
        async with use(ctx) as session:
            outcome = await session.driver.act("search_page", {"pattern": pattern, "regex": regex})
            result = outcome.message if outcome.ok else f"FAILED: {outcome.message}"
            return _noted(session, result)

    return mcp


ANONYMOUS = "anonymous"


def _owner(ctx: Context) -> str:
    """Which agent is calling.

    1. A per-agent token (AUTH_TOKENS): stable across reconnects, works on every MCP protocol.
    2. The `mcp-session-id` that clients on the 2025 handshake protocol get: one per connection.
    3. Neither (stateless protocol with a shared token or none, stdio): callers cannot be told
       apart and all count as one agent.
    """
    token = get_access_token()
    agent = token.claims.get("agent") if token is not None else None
    if agent:
        return f"agent:{agent}"
    request = ctx.request_context.request if ctx.request_context else None
    session_id = request.headers.get("mcp-session-id") if request is not None else None
    return session_owner(session_id) if session_id else ANONYMOUS


def session_owner(mcp_session_id: str) -> str:
    return f"session:{mcp_session_id}"


def _noted(session: Session, text: str) -> str:
    notice = session.take_notice()
    return f"{notice}\n\n{text}" if notice else text
