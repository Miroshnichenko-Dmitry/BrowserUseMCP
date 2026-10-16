"""Runs a single browser-use Agent and converts its history into a TaskOutcome."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Settings
from .llm import build_llm
from .models import TaskOutcome, TaskProgress
from .tasks import ProgressCallback, TaskSpec

if TYPE_CHECKING:
    from browser_use import Agent
    from browser_use.agent.views import AgentHistoryList

logger = logging.getLogger(__name__)

_IGNORED_URLS = {"", "about:blank", "chrome://newtab/"}


class BrowserAgentRunner:
    """Creates an isolated browser + agent per task and tears both down afterwards."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def __call__(
        self, task_id: str, spec: TaskSpec, report: ProgressCallback, stop_requested: asyncio.Event
    ) -> TaskOutcome:
        from browser_use import Agent
        from browser_use.browser import BrowserProfile

        s = self._settings
        workdir = Path(tempfile.mkdtemp(prefix=f"browser-task-{task_id[:8]}-"))
        artifacts_dir = Path(s.artifacts_dir) / task_id if s.artifacts_dir else None
        if artifacts_dir:
            artifacts_dir.mkdir(parents=True, exist_ok=True)

        try:
            profile = BrowserProfile(
                headless=s.browser_headless,
                executable_path=s.browser_executable_path,
                user_data_dir=workdir / "profile",  # fresh, isolated profile per task
                downloads_path=workdir / "downloads",
                allowed_domains=spec.allowed_domains or s.browser_allowed_domains,
                prohibited_domains=s.browser_prohibited_domains,
                block_ip_addresses=s.browser_block_ip_addresses,
                enable_default_extensions=s.browser_enable_extensions,
                keep_alive=False,
            )

            async def should_stop() -> bool:
                return stop_requested.is_set()

            agent_kwargs: dict[str, Any] = {
                "task": spec.task,
                "llm": build_llm(s),
                "browser_profile": profile,
                "max_failures": s.agent_max_failures,
                "max_actions_per_step": s.agent_max_actions_per_step,
                "step_timeout": s.agent_step_timeout_seconds,
                "llm_timeout": int(s.llm_timeout_seconds),
                "use_vision": s.agent_use_vision,
                "use_judge": s.agent_use_judge,
                "flash_mode": s.agent_flash_mode,
                "extend_system_message": s.agent_extend_system_message,
                "file_system_path": str(workdir / "fs"),
                "task_id": task_id,
                # The library installs SIGINT handlers by default; a server must not let it.
                "enable_signal_handler": False,
                "register_should_stop_callback": should_stop,
            }
            gif_path = artifacts_dir / "run.gif" if artifacts_dir and s.artifacts_save_gif else None
            if gif_path:
                agent_kwargs["generate_gif"] = str(gif_path)

            agent = Agent(**agent_kwargs)

            async def on_step_end(running: Agent) -> None:
                try:
                    await report(_progress_from_agent(running, spec.max_steps))
                except Exception:  # never let reporting break the agent loop
                    logger.debug("progress report failed", exc_info=True)

            history = await agent.run(max_steps=spec.max_steps, on_step_end=on_step_end)

            artifacts: list[str] = []
            if artifacts_dir:
                history_path = artifacts_dir / "history.json"
                try:
                    history.save_to_file(history_path)
                    artifacts.append(str(history_path))
                except Exception:
                    logger.warning("failed to save history", exc_info=True, extra={"task_id": task_id})
                if gif_path and gif_path.exists():
                    artifacts.append(str(gif_path))

            return outcome_from_history(history, artifacts)
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)


def _progress_from_agent(agent: Agent, max_steps: int) -> TaskProgress:
    last_output = getattr(agent.state, "last_model_output", None)
    goal = getattr(last_output, "next_goal", None) if last_output else None
    url = None
    if agent.history.history:
        url = agent.history.history[-1].state.url or None
    return TaskProgress(
        step=max(agent.state.n_steps - 1, 0),
        max_steps=max_steps,
        last_goal=goal,
        current_url=url,
    )


def _dedupe(items: list[str | None]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item and item not in _IGNORED_URLS:
            seen.setdefault(item, None)
    return list(seen)


def outcome_from_history(history: AgentHistoryList, artifacts: list[str] | None = None) -> TaskOutcome:
    """Interpret the history honestly: only `done` counts as finished.

    `final_result()` returns the last extracted content even when the agent never called `done`
    (e.g. it hit the step limit), so it must not be reported as a result in that case.
    """
    done = history.is_done()
    success = history.is_successful() if done else None
    step_errors = [e for e in history.errors() if e]
    final = history.final_result() if done else None

    error: str | None = None
    if not done:
        error = step_errors[-1] if step_errors else None
    elif success is False:
        error = "Agent reported that it could not complete the task"

    return TaskOutcome(
        done=done,
        success=success,
        result=final,
        error=error,
        steps=history.number_of_steps(),
        visited_urls=_dedupe(history.urls()),
        step_errors=step_errors,
        artifacts=artifacts or [],
    )
