"""Public data models returned by MCP tools."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT})


class TaskProgress(BaseModel):
    step: int = Field(default=0, description="Completed agent steps.")
    max_steps: int
    last_goal: str | None = Field(default=None, description="What the agent is trying to do next.")
    current_url: str | None = None


class TaskOutcome(BaseModel):
    """What the browser agent produced. Internal contract between runner and task manager."""

    done: bool = Field(description="The agent called its `done` action.")
    success: bool | None = Field(description="Agent's own verdict; None if it did not say.")
    result: str | None = None
    error: str | None = None
    steps: int = 0
    visited_urls: list[str] = Field(default_factory=list)
    step_errors: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class TaskView(BaseModel):
    """Snapshot of a browser task. `done=true` means the status is final."""

    task_id: str
    status: TaskStatus
    done: bool = Field(description="True when the task reached a final status.")
    success: bool | None = Field(default=None, description="True only for status=succeeded.")
    result: str | None = Field(default=None, description="Final answer from the agent.")
    error: str | None = None
    task: str
    progress: TaskProgress
    queue_position: int | None = Field(default=None, description="1-based position while queued.")
    visited_urls: list[str] = Field(default_factory=list)
    step_errors: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    next_action: str | None = Field(default=None, description="Instruction for the calling model.")


class TaskList(BaseModel):
    tasks: list[TaskView]
    total: int
