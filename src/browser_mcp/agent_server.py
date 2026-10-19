"""Agent endpoint (/mcp/agent): natural-language tasks run by a browser-use Agent in the background."""

# NOTE: no `from __future__ import annotations` here: FastMCP introspects real annotations.
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.lifespan import lifespan
from pydantic import Field

from . import __version__
from .browsers import resolve_domains
from .config import Settings
from .llm import check_llm_ready
from .models import TaskList, TaskStatus, TaskView
from .tasks import QueueFullError, Runner, TaskManager, TaskManagerConfig, TaskNotFoundError, TaskSpec

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Browser automation server. A natural-language task is executed by an autonomous browser agent.
Tasks take from seconds to many minutes, so they run in the background:

1. browser_run_task(task=...) starts a task and waits a bit. If the returned `done` is true,
   the task is finished: read `status`, `result`, `error`.
2. If `done` is false, call browser_wait_task(task_id=...) repeatedly until `done` is true.
   Never report a task as finished while `done` is false.
3. browser_cancel_task stops a task; browser_list_tasks shows recent tasks.

Only status=succeeded means the goal was achieved. failed / timed_out / cancelled mean it was not.
Write tasks as concrete goals with the expected output, e.g.
"Open https://example.com and return the page title".
"""

_RUN_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
_READ_ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": False}


def create_agent_server(
    settings: Settings, runner: Runner, auth: TokenVerifier | None = None
) -> tuple[FastMCP, TaskManager]:
    manager = TaskManager(
        runner,
        TaskManagerConfig(
            max_concurrent=settings.max_concurrent_tasks,
            max_queued=settings.max_queued_tasks,
            task_timeout=settings.task_timeout_seconds,
            cancel_grace=settings.task_cancel_grace_seconds,
            result_ttl=settings.task_result_ttl_seconds,
            max_stored=settings.max_stored_tasks,
            result_max_chars=settings.result_max_chars,
        ),
    )

    @lifespan
    async def app_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        await manager.start()
        ready, detail = await check_llm_ready(settings)
        if ready:
            logger.info("LLM ready", extra={"llm": detail})
        else:
            logger.warning("LLM not ready yet; tasks will fail until it is", extra={"llm": detail})
        try:
            yield {"task_manager": manager}
        finally:
            logger.info("shutting down, stopping active tasks", extra={"active": manager.active_count})
            await manager.shutdown(timeout=settings.task_cancel_grace_seconds + 5)

    mcp = FastMCP(
        name="browser-use-agent",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth,
        lifespan=app_lifespan,
        mask_error_details=True,
    )

    def clamp_wait(value: float | None) -> float:
        wait = settings.wait_default_seconds if value is None else value
        return min(max(wait, 0), settings.wait_max_seconds)

    async def wait_with_progress(task_id: str, wait: float, ctx: Context) -> TaskView:
        async def on_update(view: TaskView) -> None:
            p = view.progress
            message = f"{view.status.value}: step {p.step}/{p.max_steps}"
            if view.queue_position:
                message = f"queued (position {view.queue_position})"
            elif p.last_goal:
                message += f" — {p.last_goal[:200]}"
            await ctx.report_progress(progress=p.step, total=p.max_steps, message=message)

        return await manager.wait(task_id, timeout=wait, on_update=on_update)

    @mcp.tool(annotations=_RUN_ANNOTATIONS)
    async def browser_run_task(
        task: Annotated[
            str,
            Field(
                min_length=3,
                max_length=settings.max_task_chars,
                description="Concrete goal for the browser agent, including what to return.",
            ),
        ],
        ctx: Context,
        wait_seconds: Annotated[
            float | None,
            Field(ge=0, le=settings.wait_max_seconds, description="How long to wait for completion now."),
        ] = None,
        max_steps: Annotated[
            int | None,
            Field(ge=1, le=settings.agent_max_steps_limit, description="Agent step budget."),
        ] = None,
        allowed_domains: Annotated[
            list[str] | None,
            Field(description="Restrict navigation to these domains, e.g. ['*.wikipedia.org']."),
        ] = None,
    ) -> TaskView:
        """Start a browser task and wait for it up to `wait_seconds`.

        Returns a task snapshot. If `done` is false the task is still running:
        call browser_wait_task with the returned task_id until `done` is true.
        """
        try:
            spec = TaskSpec(
                task=task.strip(),
                max_steps=max_steps or settings.agent_max_steps,
                allowed_domains=resolve_domains(settings, allowed_domains),
            )
            view = manager.submit(spec)
        except (QueueFullError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return await wait_with_progress(view.task_id, clamp_wait(wait_seconds), ctx)

    @mcp.tool(annotations=_READ_ANNOTATIONS)
    async def browser_wait_task(
        task_id: Annotated[str, Field(min_length=1, max_length=64)],
        ctx: Context,
        wait_seconds: Annotated[
            float | None,
            Field(ge=0, le=settings.wait_max_seconds, description="Max time to block."),
        ] = None,
    ) -> TaskView:
        """Wait until the task finishes (or `wait_seconds` elapse) and return its snapshot.

        Sends MCP progress notifications on every agent step. Call again while `done` is false.
        """
        try:
            return await wait_with_progress(task_id, clamp_wait(wait_seconds), ctx)
        except TaskNotFoundError as exc:
            raise ToolError(f"Unknown task_id '{task_id}' (it may have expired)") from exc

    @mcp.tool(annotations=_READ_ANNOTATIONS)
    async def browser_get_task(task_id: Annotated[str, Field(min_length=1, max_length=64)]) -> TaskView:
        """Return the current snapshot of a task immediately, without waiting."""
        try:
            return manager.get(task_id)
        except TaskNotFoundError as exc:
            raise ToolError(f"Unknown task_id '{task_id}' (it may have expired)") from exc

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
    async def browser_cancel_task(task_id: Annotated[str, Field(min_length=1, max_length=64)]) -> TaskView:
        """Stop a queued or running task. The agent stops after its current step."""
        try:
            return await manager.cancel(task_id)
        except TaskNotFoundError as exc:
            raise ToolError(f"Unknown task_id '{task_id}' (it may have expired)") from exc

    @mcp.tool(annotations=_READ_ANNOTATIONS)
    async def browser_list_tasks(
        status: TaskStatus | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> TaskList:
        """List recent tasks, newest first, optionally filtered by status."""
        tasks = manager.list(status=status, limit=limit)
        return TaskList(tasks=tasks, total=len(tasks))

    return mcp, manager
