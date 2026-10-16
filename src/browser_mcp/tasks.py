"""In-process task manager: queueing, concurrency limits, timeouts, cancellation, retention.

Deliberately independent from browser-use and FastMCP so it can be unit-tested in isolation.
The actual work is delegated to a `Runner` callable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import TaskOutcome, TaskProgress, TaskStatus, TaskView

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[TaskProgress], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task: str
    max_steps: int
    allowed_domains: list[str] | None = None


class Runner:
    """Protocol-like base: run one task until it finishes.

    Implementations must poll `stop_requested` and stop cooperatively when it is set.
    They may also be hard-cancelled (CancelledError) after a grace period.
    """

    async def __call__(
        self, task_id: str, spec: TaskSpec, report: ProgressCallback, stop_requested: asyncio.Event
    ) -> TaskOutcome:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class TaskManagerConfig:
    max_concurrent: int = 1
    max_queued: int = 10
    task_timeout: float = 900
    cancel_grace: float = 15
    result_ttl: float = 3600
    max_stored: int = 200
    result_max_chars: int = 20000
    cleanup_interval: float = 60


class TaskNotFoundError(KeyError):
    pass


class QueueFullError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


@dataclass(eq=False)
class _Record:
    id: str
    spec: TaskSpec
    created_at: datetime = field(default_factory=_now)
    created_mono: float = field(default_factory=time.monotonic)
    status: TaskStatus = TaskStatus.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    finished_mono: float | None = None
    progress: TaskProgress | None = None
    outcome: TaskOutcome | None = None
    error: str | None = None
    cancel_requested: bool = False
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    version: int = 0
    worker: asyncio.Task[None] | None = None


class TaskManager:
    def __init__(self, runner: Runner | Callable[..., Awaitable[TaskOutcome]], config: TaskManagerConfig):
        self._runner = runner
        self._config = config
        self._records: dict[str, _Record] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._background: set[asyncio.Task[None]] = set()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closing = False

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="task-cleanup")

    async def shutdown(self, timeout: float = 30) -> None:
        """Stop accepting work, ask running tasks to stop, then cancel what is left."""
        self._closing = True
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        active = [r for r in self._records.values() if not r.status.is_terminal]
        for record in active:
            record.cancel_requested = True
            record.stop_event.set()
        workers = [r.worker for r in active if r.worker is not None]
        if workers:
            _, pending = await asyncio.wait(workers, timeout=timeout)
            for worker in pending:
                worker.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5)
        for task in list(self._background):
            task.cancel()

    # -------------------------------------------------------------- queries
    @property
    def active_count(self) -> int:
        return sum(1 for r in self._records.values() if not r.status.is_terminal)

    def get(self, task_id: str) -> TaskView:
        return self._view(self._require(task_id))

    def list(self, status: TaskStatus | None = None, limit: int = 20) -> list[TaskView]:
        records = sorted(self._records.values(), key=lambda r: r.created_mono, reverse=True)
        if status is not None:
            records = [r for r in records if r.status is status]
        return [self._view(r) for r in records[:limit]]

    # ------------------------------------------------------------- commands
    def submit(self, spec: TaskSpec) -> TaskView:
        if self._closing:
            raise QueueFullError("Server is shutting down")
        capacity = self._config.max_concurrent + self._config.max_queued
        if self.active_count >= capacity:
            raise QueueFullError(
                f"Too many active tasks ({self.active_count}/{capacity}). "
                "Wait for one to finish or cancel one."
            )
        record = _Record(id=uuid.uuid4().hex, spec=spec)
        record.progress = TaskProgress(max_steps=spec.max_steps)
        self._records[record.id] = record
        record.worker = asyncio.create_task(self._execute(record), name=f"browser-task-{record.id[:8]}")
        logger.info("task submitted", extra={"task_id": record.id, "max_steps": spec.max_steps})
        return self._view(record)

    async def cancel(self, task_id: str) -> TaskView:
        record = self._require(task_id)
        if record.status.is_terminal or record.cancel_requested:
            return self._view(record)
        record.cancel_requested = True
        record.stop_event.set()
        logger.info("task cancel requested", extra={"task_id": task_id, "status": record.status.value})
        if record.status is TaskStatus.QUEUED and record.worker is not None:
            record.worker.cancel()
        else:
            self._spawn(self._hard_cancel_after_grace(record))
        await self._notify(record)
        return self._view(record)

    async def wait(
        self,
        task_id: str,
        timeout: float,
        on_update: Callable[[TaskView], Awaitable[None]] | None = None,
    ) -> TaskView:
        """Block until the task is final or `timeout` elapses; call `on_update` on every change."""
        record = self._require(task_id)
        deadline = time.monotonic() + max(timeout, 0)
        seen = -1
        while True:
            if record.version != seen:
                seen = record.version
                if on_update is not None:
                    try:
                        await on_update(self._view(record))
                    except Exception:  # progress delivery must never break waiting
                        logger.debug("progress callback failed", exc_info=True)
            if record.status.is_terminal:
                return self._view(record)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._view(record)
            async with record.changed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        record.changed.wait_for(lambda seen=seen: record.version != seen), timeout=remaining
                    )

    # ------------------------------------------------------------ internals
    def _require(self, task_id: str) -> _Record:
        record = self._records.get(task_id)
        if record is None:
            raise TaskNotFoundError(task_id)
        return record

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _notify(self, record: _Record) -> None:
        async with record.changed:
            record.version += 1
            record.changed.notify_all()

    async def _report(self, record: _Record, progress: TaskProgress) -> None:
        record.progress = progress
        await self._notify(record)

    async def _execute(self, record: _Record) -> None:
        try:
            async with self._semaphore:
                if record.cancel_requested:
                    self._finish(record, TaskStatus.CANCELLED, error="Cancelled before start")
                    return
                record.status = TaskStatus.RUNNING
                record.started_at = _now()
                await self._notify(record)
                logger.info("task started", extra={"task_id": record.id})

                async def report(progress: TaskProgress) -> None:
                    await self._report(record, progress)

                try:
                    async with asyncio.timeout(self._config.task_timeout):
                        outcome = await self._runner(record.id, record.spec, report, record.stop_event)
                except TimeoutError:
                    self._finish(
                        record,
                        TaskStatus.TIMED_OUT,
                        error=f"Task exceeded {self._config.task_timeout:.0f}s and was stopped",
                    )
                    return
                self._apply_outcome(record, outcome)
        except asyncio.CancelledError:
            self._finish(record, TaskStatus.CANCELLED, error="Cancelled")
            raise
        except Exception as exc:
            logger.exception("task crashed", extra={"task_id": record.id})
            self._finish(record, TaskStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
        finally:
            await asyncio.shield(self._notify(record))

    def _apply_outcome(self, record: _Record, outcome: TaskOutcome) -> None:
        record.outcome = outcome
        if record.progress is not None:
            record.progress = record.progress.model_copy(update={"step": outcome.steps})
        if record.cancel_requested:
            self._finish(record, TaskStatus.CANCELLED, error="Cancelled by request")
        elif outcome.done and outcome.success is not False:
            self._finish(record, TaskStatus.SUCCEEDED)
        elif outcome.done:
            self._finish(record, TaskStatus.FAILED, error=outcome.error or "Agent reported that it failed")
        else:
            self._finish(
                record,
                TaskStatus.FAILED,
                error=outcome.error or "Agent stopped without finishing (step or failure limit reached)",
            )

    def _finish(self, record: _Record, status: TaskStatus, error: str | None = None) -> None:
        if record.status.is_terminal:
            return
        record.status = status
        record.error = error
        record.finished_at = _now()
        record.finished_mono = time.monotonic()
        logger.info(
            "task finished",
            extra={"task_id": record.id, "status": status.value, "error": error},
        )

    async def _hard_cancel_after_grace(self, record: _Record) -> None:
        await asyncio.sleep(self._config.cancel_grace)
        if not record.status.is_terminal and record.worker is not None:
            logger.warning("task ignored stop request, hard-cancelling", extra={"task_id": record.id})
            record.worker.cancel()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.cleanup_interval)
            self.purge()

    def purge(self) -> int:
        """Drop expired finished tasks and enforce the storage cap. Returns number removed."""
        now = time.monotonic()
        finished = sorted(
            (r for r in self._records.values() if r.finished_mono is not None),
            key=lambda r: r.finished_mono or 0,
        )
        doomed = {r.id for r in finished if now - (r.finished_mono or now) > self._config.result_ttl}
        overflow = len(self._records) - len(doomed) - self._config.max_stored
        for record in finished:
            if overflow <= 0:
                break
            if record.id not in doomed:
                doomed.add(record.id)
                overflow -= 1
        for task_id in doomed:
            self._records.pop(task_id, None)
        return len(doomed)

    def _queue_position(self, record: _Record) -> int | None:
        if record.status is not TaskStatus.QUEUED:
            return None
        queued = sorted(
            (r for r in self._records.values() if r.status is TaskStatus.QUEUED),
            key=lambda r: r.created_mono,
        )
        return next((i for i, r in enumerate(queued, start=1) if r is record), None)

    def _view(self, record: _Record) -> TaskView:
        outcome = record.outcome
        limit = self._config.result_max_chars
        duration = None
        if record.started_at is not None:
            end = record.finished_at or _now()
            duration = round((end - record.started_at).total_seconds(), 2)
        status = record.status
        return TaskView(
            task_id=record.id,
            status=status,
            done=status.is_terminal,
            success=(status is TaskStatus.SUCCEEDED) if status.is_terminal else None,
            result=_truncate(outcome.result, limit) if outcome else None,
            error=record.error,
            task=_truncate(record.spec.task, 500) or "",
            progress=record.progress or TaskProgress(max_steps=record.spec.max_steps),
            queue_position=self._queue_position(record),
            visited_urls=outcome.visited_urls[-20:] if outcome else [],
            step_errors=outcome.step_errors[-10:] if outcome else [],
            artifacts=outcome.artifacts if outcome else [],
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            duration_seconds=duration,
            next_action=_next_action(record),
        )


def _next_action(record: _Record) -> str:
    if record.status.is_terminal:
        if record.status is TaskStatus.SUCCEEDED:
            return "Task finished successfully. Use `result` as the answer."
        return "Task is over and did not succeed. Report `error` to the user; retry only if it makes sense."
    if record.cancel_requested:
        return f"Cancellation in progress. Call browser_wait_task(task_id='{record.id}') to confirm."
    return (
        f"Task is still {record.status.value}. Call browser_wait_task(task_id='{record.id}') "
        "until done=true. Do not tell the user it is finished yet."
    )
