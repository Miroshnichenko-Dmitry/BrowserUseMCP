"""Browser endpoint (/mcp/browser): low-level browser control driven by the client's own model.

Every action returns the fresh page state, so the client never needs a separate "look" call
between actions. Tool results are plain text (no structured duplicate) to keep the client's
context small.
"""

# NOTE: no `from __future__ import annotations` here: FastMCP introspects real annotations.
import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.lifespan import lifespan
from fastmcp.utilities.types import Image
from pydantic import Field

from . import __version__
from .browsers import PoolExhaustedError, resolve_domains
from .config import Settings
from .sessions import ActionOutcome, Session, SessionLimitError, SessionManager, SessionNotFoundError

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Low-level browser control: you drive a real Chromium step by step.

- A default browser opens automatically on the first call and closes after a period of inactivity.
  All tools act on it unless you pass `session_id`. The default browser may be shared with other
  clients of this server: for work that must not be disturbed, call browser_open and pass the
  returned session_id to every tool.
- Every action returns the new page state: URL, title, tabs and the page content where
  interactive elements are marked [index]. Use those indexes with browser_click, browser_type,
  browser_select_option. Indexes are valid only for the most recent state you received.
- browser_read returns the whole page as markdown (for reading long content).
  browser_find searches the page text. browser_screenshot shows the layout when text is not enough.
- Call browser_close when you are done to free the browser for others.
"""

SessionId = Annotated[
    str | None,
    Field(default=None, max_length=64, description="Omit to use your default browser."),
]
Index = Annotated[int, Field(ge=0, description="Element [index] from the latest page state.")]

_ACT = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
_READ = {"readOnlyHint": True, "openWorldHint": True}


def create_browser_server(
    settings: Settings, sessions: SessionManager, auth: TokenVerifier | None = None
) -> FastMCP:
    @lifespan
    async def app_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        await sessions.start()
        try:
            yield {"sessions": sessions}
        finally:
            logger.info("shutting down, closing browser sessions", extra={"sessions": sessions.count})
            await sessions.shutdown()

    mcp = FastMCP(
        name="browser-use-browser",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth,
        lifespan=app_lifespan,
        mask_error_details=True,
    )

    def tool(annotations: dict[str, Any]):
        # output_schema=None: results go out once, as text, not duplicated as structured content.
        return mcp.tool(annotations=annotations, output_schema=None)

    async def run(ctx: Context, session_id: str | None, action: str, params: dict[str, Any]) -> str:
        """Run one browser-use action and return its outcome followed by the new page state."""
        try:
            async with sessions.use(_owner(ctx), session_id) as session:
                outcome = await session.driver.act(action, params)
                return await _with_state(session, outcome)
        except (SessionNotFoundError, SessionLimitError, PoolExhaustedError) as exc:
            raise ToolError(_session_error(exc)) from exc

    async def _with_state(session: Session, outcome: ActionOutcome) -> str:
        # Let the page react (navigation, XHR) before reading it back.
        await asyncio.sleep(0.3)
        state = await session.driver.state()
        status = "OK" if outcome.ok else "FAILED"
        return f"{status}: {outcome.message}\n\n{state.text}"

    # ------------------------------------------------------------ sessions
    @tool(_ACT)
    async def browser_open(
        ctx: Context,
        url: Annotated[str | None, Field(description="Page to open right away.")] = None,
        allowed_domains: Annotated[
            list[str] | None,
            Field(description="Restrict this browser to these domains, e.g. ['*.wikipedia.org']."),
        ] = None,
    ) -> str:
        """Open an additional browser and return its session_id. Not needed for a single browser:
        every tool opens your default one automatically."""
        try:
            domains = resolve_domains(settings, allowed_domains)
            session = await sessions.open(_owner(ctx), allowed_domains=domains)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except (SessionLimitError, PoolExhaustedError) as exc:
            raise ToolError(_session_error(exc)) from exc
        header = f"session_id: {session.id}"
        if url:
            return f"{header}\n" + await run(ctx, session.id, "navigate", {"url": url})
        return header

    @tool({"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
    async def browser_close(ctx: Context, session_id: SessionId = None) -> str:
        """Close a browser (default: yours) and delete its profile, cookies and downloads."""
        target = session_id or _default_id(sessions, _owner(ctx))
        if target and await sessions.close(target):
            return f"Closed browser session {target}."
        return "No such browser session (already closed or expired)."

    @tool({"readOnlyHint": True, "openWorldHint": False})
    async def browser_list_sessions(ctx: Context) -> str:
        """List open browser sessions with their last URL and idle time."""
        rows = []
        for s in sessions.list():
            tags = [t for t, on in (("default", s.is_default), ("yours", s.owner == _owner(ctx))) if on]
            label = f" ({', '.join(tags)})" if tags else ""
            rows.append(f"{s.id}{label}: idle {s.idle_seconds:.0f}s, url {s.driver.last_url or '-'}")
        return "\n".join(rows) or "No open browser sessions."

    # ------------------------------------------------------------ navigation
    @tool(_ACT)
    async def browser_navigate(
        ctx: Context,
        url: Annotated[str, Field(min_length=1, max_length=4000)],
        new_tab: bool = False,
        session_id: SessionId = None,
    ) -> str:
        """Open a URL in the current tab (or a new one)."""
        return await run(ctx, session_id, "navigate", {"url": url, "new_tab": new_tab})

    @tool(_ACT)
    async def browser_back(ctx: Context, session_id: SessionId = None) -> str:
        """Go back in the current tab's history."""
        return await run(ctx, session_id, "go_back", {})

    @tool(_ACT)
    async def browser_tab(
        ctx: Context,
        tab_id: Annotated[
            str, Field(min_length=4, max_length=4, description="4-char id from the Tabs line.")
        ],
        close: Annotated[bool, Field(description="Close the tab instead of switching to it.")] = False,
        session_id: SessionId = None,
    ) -> str:
        """Switch to a tab, or close it."""
        return await run(ctx, session_id, "close" if close else "switch", {"tab_id": tab_id})

    # ------------------------------------------------------------ interaction
    @tool(_ACT)
    async def browser_click(
        ctx: Context,
        index: Annotated[int | None, Field(ge=0, description="Element [index]. Preferred.")] = None,
        x: Annotated[int | None, Field(ge=0, description="Viewport X, only if there is no index.")] = None,
        y: Annotated[int | None, Field(ge=0, description="Viewport Y, together with x.")] = None,
        session_id: SessionId = None,
    ) -> str:
        """Click an element by [index], or at viewport coordinates (x, y) seen on a screenshot."""
        if index is not None:
            params: dict[str, Any] = {"index": index}
        elif x is not None and y is not None:
            params = {"coordinate_x": x, "coordinate_y": y}
        else:
            raise ToolError("Pass `index`, or both `x` and `y`.")
        return await run(ctx, session_id, "click", params)

    @tool(_ACT)
    async def browser_type(
        ctx: Context,
        index: Index,
        text: Annotated[str, Field(max_length=10000)],
        clear: Annotated[bool, Field(description="Replace the current value (false = append).")] = True,
        submit: Annotated[bool, Field(description="Press Enter after typing.")] = False,
        session_id: SessionId = None,
    ) -> str:
        """Type into an input or textarea. text="" with clear=true empties the field."""
        try:
            async with sessions.use(_owner(ctx), session_id) as session:
                outcome = await session.driver.act("input", {"index": index, "text": text, "clear": clear})
                if outcome.ok and submit:
                    pressed = await session.driver.act("send_keys", {"keys": "Enter"})
                    outcome = ActionOutcome(pressed.ok, f"{outcome.message}; Enter: {pressed.message}")
                return await _with_state(session, outcome)
        except (SessionNotFoundError, SessionLimitError, PoolExhaustedError) as exc:
            raise ToolError(_session_error(exc)) from exc

    @tool(_ACT)
    async def browser_press_keys(
        ctx: Context,
        keys: Annotated[
            str, Field(min_length=1, max_length=200, description="e.g. Enter, Escape, Control+a")
        ],
        session_id: SessionId = None,
    ) -> str:
        """Press a key or shortcut in the focused element."""
        return await run(ctx, session_id, "send_keys", {"keys": keys})

    @tool(_ACT)
    async def browser_select_option(
        ctx: Context,
        index: Index,
        option: Annotated[str, Field(min_length=1, description="Exact visible text or value of the option.")],
        session_id: SessionId = None,
    ) -> str:
        """Choose an option in a <select> or custom dropdown.

        On a wrong option the error lists the valid ones.
        """
        return await run(ctx, session_id, "select_dropdown", {"index": index, "text": option})

    @tool(_ACT)
    async def browser_scroll(
        ctx: Context,
        direction: Literal["down", "up"] = "down",
        pages: Annotated[float, Field(gt=0, le=20, description="1 = one screen, 0.5 = half.")] = 1.0,
        index: Annotated[int | None, Field(ge=0, description="Scroll inside this element instead.")] = None,
        session_id: SessionId = None,
    ) -> str:
        """Scroll the page (or a scrollable element)."""
        params: dict[str, Any] = {"down": direction == "down", "pages": pages}
        if index is not None:
            params["index"] = index
        return await run(ctx, session_id, "scroll", params)

    @tool(_READ)
    async def browser_wait(
        ctx: Context,
        seconds: Annotated[float, Field(gt=0, le=30)] = 2,
        session_id: SessionId = None,
    ) -> str:
        """Wait for the page to load or update, then return its state."""
        try:
            async with sessions.use(_owner(ctx), session_id) as session:
                await asyncio.sleep(seconds)
                return await _with_state(session, ActionOutcome(True, f"waited {seconds:g}s"))
        except (SessionNotFoundError, SessionLimitError, PoolExhaustedError) as exc:
            raise ToolError(_session_error(exc)) from exc

    # ------------------------------------------------------------ reading
    @tool(_READ)
    async def browser_snapshot(
        ctx: Context,
        screenshot: Annotated[
            bool, Field(description="Also return a screenshot with the [index] boxes drawn on it.")
        ] = False,
        session_id: SessionId = None,
    ) -> list[Any]:
        """Return the current page state without doing anything."""
        try:
            async with sessions.use(_owner(ctx), session_id) as session:
                state = await session.driver.state(screenshot=screenshot)
        except (SessionNotFoundError, SessionLimitError, PoolExhaustedError) as exc:
            raise ToolError(_session_error(exc)) from exc
        content: list[Any] = [state.text]
        if state.screenshot:
            content.append(Image(data=state.screenshot, format="jpeg"))
        return content

    @tool(_READ)
    async def browser_screenshot(
        ctx: Context,
        full_page: bool = False,
        session_id: SessionId = None,
    ) -> list[Any]:
        """Screenshot of the visible area (or the whole page). Coordinates match browser_click x/y."""
        try:
            async with sessions.use(_owner(ctx), session_id) as session:
                data = await session.driver.screenshot(full_page=full_page)
                url = session.driver.last_url
        except (SessionNotFoundError, SessionLimitError, PoolExhaustedError) as exc:
            raise ToolError(_session_error(exc)) from exc
        return [f"Screenshot of {url or 'the current page'}", Image(data=data, format="jpeg")]

    @tool(_READ)
    async def browser_read(
        ctx: Context,
        start: Annotated[int, Field(ge=0, description="Character offset, to continue a long page.")] = 0,
        session_id: SessionId = None,
    ) -> str:
        """Return the page content as markdown, with links. Long pages come in chunks: pass `start`."""
        try:
            async with sessions.use(_owner(ctx), session_id) as session:
                text = await session.driver.read()
                url = session.driver.last_url
        except (SessionNotFoundError, SessionLimitError, PoolExhaustedError) as exc:
            raise ToolError(_session_error(exc)) from exc
        limit = settings.browser_read_max_chars
        chunk = text[start : start + limit]
        end = start + len(chunk)
        header = f"URL: {url or '-'}\nChars {start}-{end} of {len(text)}"
        if end < len(text):
            header += f" (more: call browser_read with start={end})"
        return f"{header}\n\n{chunk}"

    @tool(_READ)
    async def browser_find(
        ctx: Context,
        pattern: Annotated[str, Field(min_length=1, max_length=500)],
        regex: bool = False,
        session_id: SessionId = None,
    ) -> str:
        """Find text on the page (like Ctrl+F) and return the matches with surrounding context."""
        try:
            async with sessions.use(_owner(ctx), session_id) as session:
                outcome = await session.driver.act("search_page", {"pattern": pattern, "regex": regex})
        except (SessionNotFoundError, SessionLimitError, PoolExhaustedError) as exc:
            raise ToolError(_session_error(exc)) from exc
        return outcome.message if outcome.ok else f"FAILED: {outcome.message}"

    return mcp


SHARED_OWNER = "shared"


def _owner(ctx: Context) -> str:
    """Who owns the default browser.

    Clients on the 2025 protocol get an `mcp-session-id`, so each connection has its own default
    browser. The newer stateless protocol, stdio and in-memory transports carry no per-client
    identity (`ctx.session_id` is regenerated per request there), so those clients share one
    default browser; `browser_open` + explicit `session_id` gives them an isolated one.
    """
    request = ctx.request_context.request if ctx.request_context else None
    session_id = request.headers.get("mcp-session-id") if request is not None else None
    return session_id or SHARED_OWNER


def _default_id(sessions: SessionManager, owner: str) -> str | None:
    return next((s.id for s in sessions.list() if s.owner == owner and s.is_default), None)


def _session_error(exc: Exception) -> str:
    if isinstance(exc, SessionNotFoundError):
        return (
            f"Unknown browser session {exc.args[0]!r} (closed or expired). Omit session_id to use a new one."
        )
    return str(exc)
